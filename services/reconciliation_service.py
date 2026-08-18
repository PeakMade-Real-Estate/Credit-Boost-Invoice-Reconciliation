"""
Core reconciliation service.

This module contains ``run_reconciliation()``, the single entry point for the
entire reconciliation engine.  It is completely independent of Flask, HTTP,
and HTML templates.

The orchestrator selects a ``VendorReconciliationStrategy`` based on
``vendor_code`` and delegates all vendor-specific logic to that strategy.
Shared stages (cash-report parsing, cash join, portfolio totals, validation,
status determination) run the same way for every vendor.

Example::

    from services.reconciliation_service import run_reconciliation

    result = run_reconciliation(
        invoice_file_path="invoice.xlsx",
        cash_report_file_path="cash_report.csv",
        reporting_month="2026-07",
        vendor_code="rent_plus",
    )
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Dict, List, Optional

from models.reconciliation_models import (
    CashRecord,
    ExceptionSeverity,
    PortfolioTotals,
    PropertyException,
    PropertyResult,
    ReconciliationResult,
    ReconciliationStatus,
)
from services.cash_report_parser import parse_cash_report
from services.property_master_loader import load_property_master
from services.validation_service import run_all_validations
from services.vendor_strategy import get_vendor_strategy, normalize_vendor_code

logger = logging.getLogger(__name__)


def run_reconciliation(
    invoice_file_path: str,
    cash_report_file_path: str,
    reporting_month: str,
    vendor_code: Optional[str] = None,
    vendor: Optional[str] = None,
    *,
    property_master_path: str = "data/property_master.csv",
    rate_mapping_path: Optional[str] = None,
    property_aliases_path: Optional[str] = None,
    property_rollups_path: Optional[str] = None,
    invoice_tolerance: Decimal = Decimal("0.01"),
    balance_tolerance: Decimal = Decimal("0.05"),
    fuzzy_threshold: int = 80,
    manual_property_overrides: Optional[Dict[str, str]] = None,
    manual_rate_overrides: Optional[Dict[int, str]] = None,
    known_file_hashes: Optional[List[str]] = None,
    uploaded_by: str = "system",
    charge_code: Optional[str] = None,
    run_id: Optional[str] = None,
    generate_outputs: bool = False,
    output_folder: str = "outputs",
) -> ReconciliationResult:
    """Run the full invoice reconciliation and return a structured result."""
    effective_code = vendor_code or (vendor and normalize_vendor_code(vendor)) or ""
    if not effective_code:
        raise ValueError("Either vendor_code or vendor must be provided.")

    strategy = get_vendor_strategy(effective_code)

    effective_run_id = (
        run_id
        or f"REC-{reporting_month.replace('-', '')}-{str(uuid.uuid4()).split('-')[0].upper()}"
    )
    errors: list = []

    logger.info(
        "Starting reconciliation run_id=%s  month=%s  vendor=%s",
        effective_run_id, reporting_month, strategy.display_name,
    )

    # Stage 1 - Parse vendor file
    try:
        raw_lines, invoice_summary = strategy.parse_vendor_file(
            invoice_file_path, reporting_month
        )
    except Exception as exc:
        logger.error("Invoice parsing failed: %s", exc)
        return ReconciliationResult(
            status=ReconciliationStatus.FAILED,
            reporting_month=reporting_month,
            vendor=strategy.display_name,
            run_id=effective_run_id,
            errors=[f"Invoice parsing failed: {exc}"],
        )

    # Stage 2 - Normalise vendor data
    normalized_lines = strategy.normalize_vendor_data(raw_lines)

    # Stage 3 - Vendor-specific pre-matching validation
    vendor_validations = strategy.validate_vendor_data(normalized_lines, invoice_summary)

    # Stage 4 - Load property master (shared)
    try:
        property_master = load_property_master(property_master_path)
    except Exception as exc:
        logger.error("Property master loading failed: %s", exc)
        return ReconciliationResult(
            status=ReconciliationStatus.FAILED,
            reporting_month=reporting_month,
            vendor=strategy.display_name,
            run_id=effective_run_id,
            invoice_summary=invoice_summary,
            errors=[f"Property master loading failed: {exc}"],
        )

    # Stage 5 - Load vendor reference data
    try:
        vendor_ref = strategy.load_vendor_reference_data(
            rate_mapping_path=rate_mapping_path,
            property_aliases_path=property_aliases_path,
            property_rollups_path=property_rollups_path,
        )
    except Exception as exc:
        logger.error("Vendor reference data loading failed: %s", exc)
        return ReconciliationResult(
            status=ReconciliationStatus.FAILED,
            reporting_month=reporting_month,
            vendor=strategy.display_name,
            run_id=effective_run_id,
            invoice_summary=invoice_summary,
            errors=[f"Vendor reference data loading failed: {exc}"],
        )

    # Stage 6 - Match to properties
    matched_lines, property_exceptions = strategy.match_to_properties(
        normalized_lines, property_master, vendor_ref, reporting_month,
        fuzzy_threshold=fuzzy_threshold, manual_overrides=manual_property_overrides,
    )

    # Stage 7 - Aggregate + vendor qty/rate calculation
    property_results, rate_exceptions = strategy.aggregate_and_rate_map(
        matched_lines, property_master, vendor_ref, reporting_month,
        manual_rate_overrides=manual_rate_overrides,
    )

    # Stage 8 - Parse cash report (shared)
    try:
        cash_records = parse_cash_report(
            cash_report_file_path,
            reporting_month=reporting_month,
            charge_code=charge_code,
        )
    except Exception as exc:
        logger.error("Cash report parsing failed: %s", exc)
        return ReconciliationResult(
            status=ReconciliationStatus.FAILED,
            reporting_month=reporting_month,
            vendor=strategy.display_name,
            run_id=effective_run_id,
            invoice_summary=invoice_summary,
            errors=[f"Cash report parsing failed: {exc}"],
        )

    # Stage 9 - Join cash (shared)
    property_results, cash_exceptions = _join_cash_to_properties(
        property_results, cash_records, property_master
    )
    for exc in cash_exceptions:
        property_exceptions.append(exc)

    # Stage 10 - Property financials
    property_results = strategy.calculate_property_financials(
        property_results, matched_lines, vendor_ref
    )

    # Stage 11 - Portfolio totals (shared)
    portfolio_totals = _calculate_portfolio_totals(property_results)

    # Stage 12 - Validation (shared + vendor-specific)
    validation_results = run_all_validations(
        invoice_lines=[],
        cash_records=cash_records,
        property_results=property_results,
        invoice_summary=invoice_summary,
        portfolio_totals=portfolio_totals,
        reporting_month=reporting_month,
        invoice_file_hash=invoice_summary.source_file_hash if invoice_summary else "",
        cash_file_hash=cash_records[0].source_file_name if cash_records else "",
        invoice_tolerance=invoice_tolerance,
        balance_tolerance=balance_tolerance,
        known_hashes=known_file_hashes or [],
    )
    validation_results = vendor_validations + validation_results

    # Stage 13 - Determine status (shared)
    status = _determine_status(property_exceptions, rate_exceptions, validation_results)
    portfolio_totals.overall_status = status

    # Split vendor lines by type for type-safe storage
    from models.reconciliation_models import InvoiceLine
    invoice_lines_typed = [l for l in matched_lines if isinstance(l, InvoiceLine)]
    vendor_lines_other = [l for l in matched_lines if not isinstance(l, InvoiceLine)]

    result = ReconciliationResult(
        status=status,
        reporting_month=reporting_month,
        vendor=strategy.display_name,
        run_id=effective_run_id,
        invoice_summary=invoice_summary,
        property_results=property_results,
        property_exceptions=property_exceptions,
        rate_exceptions=rate_exceptions,
        validation_results=validation_results,
        portfolio_totals=portfolio_totals,
        invoice_lines=invoice_lines_typed,
        vendor_transaction_lines=vendor_lines_other,
        cash_records=cash_records,
        errors=errors,
    )

    # Stage 14 (optional) - Generate Excel outputs
    if generate_outputs:
        try:
            from services.output_generator import generate_outputs as gen
            from services.output_generator import generate_reconciliation_workbook
            accounting_path, audit_path = gen(result, output_folder)
            result.accounting_output_path = accounting_path
            result.audit_output_path = audit_path
            result.reconciliation_csv_path = generate_reconciliation_workbook(result, output_folder)
        except Exception as exc:
            logger.error("Output generation failed: %s", exc)
            result.errors.append(f"Output generation failed: {exc}")

    logger.info(
        "Reconciliation complete: run_id=%s status=%s  properties=%d  exceptions=%d",
        effective_run_id, status.value, len(property_results), result.exception_count,
    )
    return result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _join_cash_to_properties(
    property_results: List[PropertyResult],
    cash_records: List[CashRecord],
    property_master: list,
) -> tuple:
    """Match cash records to property results; return (results, exceptions)."""
    from models.reconciliation_models import ExceptionType

    pm_by_pms: Dict[str, object] = {
        p.pms_property_id: p for p in property_master if p.pms_property_id
    }
    cash_by_pms: Dict[str, CashRecord] = {
        r.pms_property_id: r for r in cash_records if r.pms_property_id
    }
    cash_by_internal: Dict[str, CashRecord] = {
        r.internal_property_id: r for r in cash_records if r.internal_property_id
    }
    # Name-based fallback index (used when PMS IDs are not yet populated in the PM)
    from services.utils import normalize_text as _nt
    cash_by_norm_name: Dict[str, CashRecord] = {
        _nt(r.property_name): r for r in cash_records if r.property_name
    }
    for rec in cash_records:
        if not rec.internal_property_id and rec.pms_property_id:
            pm = pm_by_pms.get(rec.pms_property_id)
            if pm:
                rec.internal_property_id = pm.internal_property_id
                cash_by_internal[pm.internal_property_id] = rec

    exceptions: List[PropertyException] = []
    matched_cash_ids: set = set()

    for pr in property_results:
        cash = cash_by_internal.get(pr.internal_property_id)
        if cash is None:
            cash = cash_by_pms.get(pr.pms_property_id)
        # Fallback: match by normalised property name when PMS IDs are absent
        if cash is None:
            cash = cash_by_norm_name.get(_nt(pr.property_name))

        if cash is None:
            pr.cash_received = Decimal("0")
            exceptions.append(PropertyException(
                exception_type=ExceptionType.NO_CASH_RECORD,
                severity=ExceptionSeverity.WARNING,
                invoice_property_name=pr.property_name,
                suggested_property_id=pr.internal_property_id,
                message=f"No cash record found for {pr.property_name!r} ({pr.internal_property_id}).",
            ))
            continue

        pr.cash_received = cash.cash_received
        matched_cash_ids.add(cash.pms_property_id)

        if cash.cash_received == Decimal("0"):
            exceptions.append(PropertyException(
                exception_type=ExceptionType.ZERO_CASH,
                severity=ExceptionSeverity.WARNING,
                invoice_property_name=pr.property_name,
                message=f"{pr.property_name!r} has zero cash received.",
            ))
        elif cash.cash_received < Decimal("0"):
            exceptions.append(PropertyException(
                exception_type=ExceptionType.NEGATIVE_CASH,
                severity=ExceptionSeverity.BLOCKING,
                invoice_property_name=pr.property_name,
                message=f"{pr.property_name!r} has negative cash received: {cash.cash_received}.",
            ))
        elif cash.cash_received < pr.invoice_amount_owed:
            exceptions.append(PropertyException(
                exception_type=ExceptionType.CASH_BELOW_OWED,
                severity=ExceptionSeverity.WARNING,
                invoice_property_name=pr.property_name,
                message=f"{pr.property_name!r}: cash {cash.cash_received} < amount owed {pr.invoice_amount_owed}.",
            ))

    for rec in cash_records:
        if rec.pms_property_id not in matched_cash_ids:
            exceptions.append(PropertyException(
                exception_type=ExceptionType.NO_INVOICE_RECORD,
                severity=ExceptionSeverity.INFO,
                invoice_property_name=rec.property_name,
                message=f"Cash record for {rec.property_name!r} ({rec.pms_property_id}) has no matching invoice line.",
            ))

    return property_results, exceptions


def _calculate_portfolio_totals(
    property_results: List[PropertyResult],
) -> PortfolioTotals:
    totals = PortfolioTotals()
    totals.total_properties = len(property_results)
    for pr in property_results:
        totals.total_net_policy_quantity += pr.net_policy_quantity
        totals.total_invoice_amount_owed += pr.invoice_amount_owed
        totals.total_cash_received += pr.cash_received
        totals.total_actual_property_revenue_share += pr.actual_property_revenue_share
        totals.total_expected_peak_revenue_share += pr.expected_peak_revenue_share
    totals.balance_difference = (
        totals.total_cash_received
        - totals.total_invoice_amount_owed
        - totals.total_actual_property_revenue_share
    )
    return totals


def _determine_status(
    property_exceptions: list,
    rate_exceptions: list,
    validation_results: list,
) -> ReconciliationStatus:
    has_blocking = any(
        e.severity == ExceptionSeverity.BLOCKING
        for e in property_exceptions + rate_exceptions
    ) or any(
        not v.passed and v.severity == ExceptionSeverity.BLOCKING
        for v in validation_results
    )
    has_warning = any(
        e.severity == ExceptionSeverity.WARNING
        for e in property_exceptions + rate_exceptions
    ) or any(
        not v.passed and v.severity == ExceptionSeverity.WARNING
        for v in validation_results
    )
    if has_blocking:
        return ReconciliationStatus.REQUIRES_REVIEW
    if has_warning:
        return ReconciliationStatus.PASSED_WITH_WARNINGS
    return ReconciliationStatus.PASSED
