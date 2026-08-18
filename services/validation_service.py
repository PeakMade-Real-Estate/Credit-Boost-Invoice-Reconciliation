"""
Validation service.

Runs structured checks against the reconciliation result and returns a list
of ``ValidationResult`` objects.  All checks are independent of Flask.

Checks performed:

  1. Invoice total balance
  2. Cash report period consistency
  3. Duplicate file detection (by hash)
  4. Portfolio balance control
  5. Duplicate final property records

Usage::

    from services.validation_service import run_all_validations

    results = run_all_validations(
        invoice_lines=lines,
        cash_records=cash_records,
        property_results=property_results,
        invoice_summary=summary,
        portfolio_totals=portfolio,
        reporting_month="2026-07",
        invoice_file_hash="abc123",
        cash_file_hash="def456",
        invoice_tolerance=Decimal("0.01"),
        balance_tolerance=Decimal("0.05"),
    )
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Optional

from models.reconciliation_models import (
    CashRecord,
    ExceptionSeverity,
    InvoiceLine,
    InvoiceSummary,
    PortfolioTotals,
    PropertyResult,
    ValidationResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_all_validations(
    *,
    invoice_lines: List[InvoiceLine],
    cash_records: List[CashRecord],
    property_results: List[PropertyResult],
    invoice_summary: Optional[InvoiceSummary],
    portfolio_totals: Optional[PortfolioTotals],
    reporting_month: str,
    invoice_file_hash: str,
    cash_file_hash: str,
    invoice_tolerance: Decimal = Decimal("0.01"),
    balance_tolerance: Decimal = Decimal("0.05"),
    known_hashes: Optional[List[str]] = None,
) -> List[ValidationResult]:
    """Run all validation checks and return results.

    Args:
        invoice_lines:     Parsed (and matched/rate-mapped) invoice lines.
        cash_records:      Parsed cash records.
        property_results:  Aggregated per-property results.
        invoice_summary:   Summary produced by the invoice parser.
        portfolio_totals:  Portfolio-level aggregated totals.
        reporting_month:   YYYY-MM string.
        invoice_file_hash: SHA-256 of the invoice file.
        cash_file_hash:    SHA-256 of the cash report file.
        invoice_tolerance: Maximum allowed invoice difference.
        balance_tolerance: Maximum allowed portfolio balance difference.
        known_hashes:      Optional list of previously seen file hashes (for
                           duplicate detection).

    Returns:
        List of ``ValidationResult`` objects.
    """
    results: List[ValidationResult] = []

    results.append(
        _check_invoice_total(invoice_summary, invoice_tolerance)
    )
    results.append(
        _check_duplicate_file(
            invoice_file_hash, cash_file_hash, known_hashes or []
        )
    )
    results.append(
        _check_portfolio_balance(portfolio_totals, balance_tolerance)
    )
    results.append(
        _check_duplicate_property_records(property_results)
    )
    results.append(
        _check_reporting_month_present(reporting_month)
    )
    results.extend(
        _check_cash_period(cash_records, reporting_month)
    )

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    logger.info(
        "Validation complete: %d passed, %d failed / warnings.", passed, failed
    )
    return results


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_invoice_total(
    summary: Optional[InvoiceSummary],
    tolerance: Decimal,
) -> ValidationResult:
    if summary is None:
        return ValidationResult(
            check_name="invoice_total_balance",
            passed=False,
            severity=ExceptionSeverity.BLOCKING,
            message="Invoice summary not available.",
        )

    diff = abs(summary.invoice_difference)
    passed = diff <= tolerance
    return ValidationResult(
        check_name="invoice_total_balance",
        passed=passed,
        severity=ExceptionSeverity.BLOCKING if not passed else ExceptionSeverity.INFO,
        expected_value=str(summary.source_invoice_total),
        actual_value=str(summary.calculated_invoice_total),
        difference=str(summary.invoice_difference),
        message=(
            "Invoice total matches source total."
            if passed
            else (
                f"Invoice total mismatch: source={summary.source_invoice_total}, "
                f"calculated={summary.calculated_invoice_total}, "
                f"difference={summary.invoice_difference} "
                f"(tolerance={tolerance})."
            )
        ),
    )


def _check_duplicate_file(
    invoice_hash: str,
    cash_hash: str,
    known_hashes: List[str],
) -> ValidationResult:
    duplicate_found = invoice_hash in known_hashes or cash_hash in known_hashes
    return ValidationResult(
        check_name="duplicate_file_detection",
        passed=not duplicate_found,
        severity=(
            ExceptionSeverity.BLOCKING if duplicate_found else ExceptionSeverity.INFO
        ),
        message=(
            "Duplicate file detected: one or more uploaded files have been "
            "processed in a previous run."
            if duplicate_found
            else "No duplicate files detected."
        ),
    )


def _check_portfolio_balance(
    totals: Optional[PortfolioTotals],
    tolerance: Decimal,
) -> ValidationResult:
    if totals is None:
        return ValidationResult(
            check_name="portfolio_balance_control",
            passed=False,
            severity=ExceptionSeverity.WARNING,
            message="Portfolio totals not available.",
        )

    diff = abs(totals.balance_difference)
    passed = diff <= tolerance
    return ValidationResult(
        check_name="portfolio_balance_control",
        passed=passed,
        severity=(
            ExceptionSeverity.BLOCKING if diff > tolerance * Decimal("10")
            else ExceptionSeverity.WARNING if not passed
            else ExceptionSeverity.INFO
        ),
        expected_value="0.00",
        actual_value=str(totals.balance_difference),
        difference=str(totals.balance_difference),
        message=(
            "Portfolio balance control passed."
            if passed
            else (
                f"Portfolio balance difference {totals.balance_difference} "
                f"exceeds tolerance {tolerance}.  "
                "Check: Cash Received - Invoice Owed - Property Revenue Share = 0"
            )
        ),
    )


def _check_duplicate_property_records(
    property_results: List[PropertyResult],
) -> ValidationResult:
    seen: set = set()
    duplicates = []
    for pr in property_results:
        key = (pr.internal_property_id, pr.reporting_month)
        if key in seen:
            duplicates.append(pr.internal_property_id)
        seen.add(key)

    passed = len(duplicates) == 0
    return ValidationResult(
        check_name="duplicate_final_property_records",
        passed=passed,
        severity=(
            ExceptionSeverity.BLOCKING if not passed else ExceptionSeverity.INFO
        ),
        actual_value=duplicates or None,
        message=(
            "No duplicate property records."
            if passed
            else f"Duplicate property records found: {duplicates}"
        ),
    )


def _check_reporting_month_present(reporting_month: str) -> ValidationResult:
    from services.utils import validate_reporting_month

    valid = bool(reporting_month) and validate_reporting_month(reporting_month)
    return ValidationResult(
        check_name="reporting_month_present",
        passed=valid,
        severity=(
            ExceptionSeverity.BLOCKING if not valid else ExceptionSeverity.INFO
        ),
        actual_value=reporting_month,
        message=(
            f"Reporting month {reporting_month!r} is valid."
            if valid
            else (
                f"Reporting month {reporting_month!r} is missing or invalid.  "
                "Expected format: YYYY-MM"
            )
        ),
    )


def _check_cash_period(
    cash_records: List[CashRecord],
    expected_month: str,
) -> List[ValidationResult]:
    """Verify that cash records carry the expected reporting month."""
    mismatched = [
        r.property_name
        for r in cash_records
        if r.reporting_month and r.reporting_month != expected_month
    ]
    passed = len(mismatched) == 0
    return [
        ValidationResult(
            check_name="cash_report_period_consistency",
            passed=passed,
            severity=(
                ExceptionSeverity.BLOCKING if not passed else ExceptionSeverity.INFO
            ),
            expected_value=expected_month,
            actual_value=(
                list({r.reporting_month for r in cash_records}) if not passed else expected_month
            ),
            message=(
                f"Cash report period matches reporting month {expected_month!r}."
                if passed
                else (
                    f"Some cash records have a different reporting month than "
                    f"{expected_month!r}.  "
                    f"Affected properties: {mismatched[:5]}"
                )
            ),
        )
    ]
