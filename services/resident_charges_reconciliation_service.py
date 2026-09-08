"""
Resident Charges vs Boom Invoice reconciliation service.

Cross-references the Entrata Resident Charges report (multi-sheet XLSX)
against a Boom invoice export to produce per-resident discrepancies:

  - ``charged_not_billed`` – resident appears in Entrata with amount > 0
    but has no matching Boom invoice line for the same property.
  - ``billed_not_charged`` – Boom billed a resident for a property but
    that resident either does not appear in Entrata or has amount = 0.

Property matching uses normalized property names.
Resident matching uses normalized resident names (converts "Last, First"
from Entrata to "First Last" before comparing).

Usage::

    from services.resident_charges_reconciliation_service import (
        run_resident_charges_reconciliation,
    )

    result = run_resident_charges_reconciliation(
        charges_file_path="August Resident Charges.xlsx",
        invoice_file_path="boom_export.csv",
        reporting_month="2026-08",
        run_id="REC-202608-CREDITBOOSTBOOM-0001",
    )
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from models.resident_charges_models import (
    BoomResidentRecord,
    PropertyChargesummary,
    ResidentChargesReconciliationResult,
    ResidentDiscrepancy,
)
from services.resident_charges_parser import parse_resident_charges
from services.utils import normalize_text

logger = logging.getLogger(__name__)

# Qualifying filters matching credit_boost_boom.yaml
_QUALIFYING_CATEGORIES: Set[str] = {"partner_boom_report_ongoing_fee"}
_QUALIFYING_TYPES: Set[str] = {"invoice"}
_QUALIFYING_TEMPLATES: Set[str] = {"boomreport"}
_QUALIFYING_SUBJECTS: Set[str] = {"boom::reportingaccount"}


def _normalize_boom_name(raw_name: str) -> str:
    """Normalize a Boom "First Last" resident name for matching."""
    if not raw_name:
        return ""
    name = re.sub(r"\([^)]*\)", "", raw_name).strip()
    return normalize_text(name)


def _parse_boom_residents(
    file_path: str,
    reporting_month: str,
) -> List[BoomResidentRecord]:
    """Parse qualifying resident-level lines from a Boom invoice export.

    Applies the same qualifying filters as the standard Boom reconciliation
    strategy so only billable resident transactions are considered.
    """
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pandas is required.  Run: pip install pandas") from exc

    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(str(path), dtype=str, keep_default_na=False)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(str(path), dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported Boom file format: {suffix}")

    # Normalize column names
    df.columns = [c.strip().lower() for c in df.columns]

    # Resolve column names flexibly
    def _col(candidates: List[str]) -> Optional[str]:
        for c in candidates:
            if c in df.columns:
                return c
        return None

    col_name = _col(["name"])
    col_property = _col(["property name", "property_name"])
    col_unit = _col(["unit"])
    col_amount = _col(["amount"])
    col_category = _col(["category"])
    col_type = _col(["transaction type", "transaction_type"])
    col_template = _col(["template name", "template_name"])
    col_subject = _col(["subject type", "subject_type"])
    col_id = _col(["id", "transaction id", "transaction_id"])

    records: List[BoomResidentRecord] = []

    for _, row in df.iterrows():
        # Apply qualifying filters
        if col_category and normalize_text(str(row.get(col_category, ""))) not in _QUALIFYING_CATEGORIES:
            continue
        if col_type and normalize_text(str(row.get(col_type, ""))) not in _QUALIFYING_TYPES:
            continue
        if col_template and normalize_text(str(row.get(col_template, ""))) not in _QUALIFYING_TEMPLATES:
            continue
        if col_subject and normalize_text(str(row.get(col_subject, ""))) not in _QUALIFYING_SUBJECTS:
            continue

        resident_name = str(row.get(col_name, "")).strip() if col_name else ""
        property_name = str(row.get(col_property, "")).strip() if col_property else ""
        unit = str(row.get(col_unit, "")).strip() if col_unit else ""
        tx_id = str(row.get(col_id, "")).strip() if col_id else str(uuid.uuid4())

        raw_amount = str(row.get(col_amount, "0")).strip() if col_amount else "0"
        # Remove currency symbols, parentheses, commas (e.g. "($2)" → "-2")
        raw_amount = raw_amount.replace("$", "").replace(",", "")
        if raw_amount.startswith("(") and raw_amount.endswith(")"):
            raw_amount = "-" + raw_amount[1:-1]
        try:
            amount = Decimal(raw_amount)
        except Exception:
            amount = Decimal("0")

        if not resident_name or not property_name:
            continue

        records.append(
            BoomResidentRecord(
                property_name=property_name,
                unit=unit,
                resident_name=resident_name,
                normalized_name=_normalize_boom_name(resident_name),
                amount=amount,
                transaction_id=tx_id,
            )
        )

    logger.info(
        "Parsed %d qualifying Boom resident records from %s",
        len(records),
        path.name,
    )
    return records


def run_resident_charges_reconciliation(
    charges_file_path: str,
    invoice_file_path: str,
    reporting_month: str,
    *,
    run_id: Optional[str] = None,
    vendor: str = "Credit Boost powered by Boom",
    charges_file_name: str = "",
    invoice_file_name: str = "",
) -> ResidentChargesReconciliationResult:
    """Cross-reference Resident Charges against the Boom invoice.

    Args:
        charges_file_path: Path to the multi-sheet Resident Charges XLSX.
        invoice_file_path: Path to the Boom invoice CSV/XLSX.
        reporting_month:   Reporting period in ``YYYY-MM`` format.
        run_id:            Optional run identifier; auto-generated if omitted.
        vendor:            Display name for the vendor (informational).
        charges_file_name: Original upload filename for the charges report.
        invoice_file_name: Original upload filename for the invoice.

    Returns:
        A fully populated ``ResidentChargesReconciliationResult``.
    """
    effective_run_id = run_id or f"RC-{reporting_month.replace('-', '')}-{uuid.uuid4().hex[:8].upper()}"

    # ------------------------------------------------------------------
    # 1. Parse both files
    # ------------------------------------------------------------------
    charge_records, _period = parse_resident_charges(charges_file_path)
    boom_records = _parse_boom_residents(invoice_file_path, reporting_month)

    # ------------------------------------------------------------------
    # 2. Index records by normalized property name
    # ------------------------------------------------------------------
    # Charges: only rows with amount > 0 count as "charged"
    charges_by_prop: Dict[str, List] = defaultdict(list)
    for rec in charge_records:
        charges_by_prop[normalize_text(rec.property_name)].append(rec)

    boom_by_prop: Dict[str, List[BoomResidentRecord]] = defaultdict(list)
    for rec in boom_records:
        boom_by_prop[normalize_text(rec.property_name)].append(rec)

    charge_prop_keys = set(charges_by_prop.keys())
    boom_prop_keys = set(boom_by_prop.keys())

    matched_props = charge_prop_keys & boom_prop_keys
    unmatched_charge_props = sorted(
        charges_by_prop[k][0].property_name
        for k in charge_prop_keys - boom_prop_keys
    )
    unmatched_boom_props = sorted(
        boom_by_prop[k][0].property_name
        for k in boom_prop_keys - charge_prop_keys
    )

    if unmatched_charge_props:
        logger.warning(
            "Properties in Resident Charges with no match in Boom invoice: %s",
            unmatched_charge_props,
        )
    if unmatched_boom_props:
        logger.warning(
            "Properties in Boom invoice with no match in Resident Charges: %s",
            unmatched_boom_props,
        )

    # ------------------------------------------------------------------
    # 3. Per-property resident matching
    # ------------------------------------------------------------------
    all_discrepancies: List[ResidentDiscrepancy] = []
    property_summaries: List[PropertyChargesummary] = []

    total_charged = 0
    total_billed = 0
    total_matched = 0
    total_cnb = 0  # charged_not_billed
    total_bnc = 0  # billed_not_charged

    for norm_prop in sorted(matched_props):
        prop_charges = charges_by_prop[norm_prop]
        prop_boom = boom_by_prop[norm_prop]
        display_name = prop_charges[0].property_name if prop_charges else norm_prop

        # Build lookup sets by normalized name
        charged_residents: Dict[str, List] = defaultdict(list)
        for r in prop_charges:
            if r.amount > 0:
                charged_residents[r.normalized_name].append(r)

        billed_residents: Dict[str, List[BoomResidentRecord]] = defaultdict(list)
        for r in prop_boom:
            billed_residents[r.normalized_name].append(r)

        charged_names = set(charged_residents.keys())
        billed_names = set(billed_residents.keys())

        matched_names = charged_names & billed_names
        cnb_names = charged_names - billed_names   # charged but NOT billed
        bnc_names = billed_names - charged_names   # billed but NOT charged

        # Build discrepancy record lists
        cnb_records = [r for name in cnb_names for r in charged_residents[name]]
        bnc_records = [r for name in bnc_names for r in billed_residents[name]]
        matched_count = len(matched_names)

        # Use raw record counts (not unique-name counts) for totals so that
        # two residents with the same name are each counted separately.
        n_charged = sum(len(v) for v in charged_residents.values())
        n_billed = sum(len(v) for v in billed_residents.values())
        n_cnb = len(cnb_records)
        n_bnc = len(bnc_records)
        for rec in cnb_records:
            all_discrepancies.append(
                ResidentDiscrepancy(
                    discrepancy_type="charged_not_billed",
                    property_name=display_name,
                    unit=rec.unit,
                    resident_name=rec.resident_name,
                    lease_status=rec.lease_status,
                    charged_amount=rec.amount,
                    billed_amount=Decimal("0"),
                )
            )

        for rec in bnc_records:
            all_discrepancies.append(
                ResidentDiscrepancy(
                    discrepancy_type="billed_not_charged",
                    property_name=display_name,
                    unit=rec.unit,
                    resident_name=rec.resident_name,
                    lease_status="",
                    charged_amount=Decimal("0"),
                    billed_amount=rec.amount,
                )
            )

        prop_total_charged = sum(r.amount for r in prop_charges if r.amount > 0)
        prop_total_billed = sum(
            abs(r.amount) for r in prop_boom
        )

        property_summaries.append(
            PropertyChargesummary(
                property_name=display_name,
                residents_charged=n_charged,
                residents_billed=n_billed,
                matched_count=matched_count,
                charged_not_billed_count=n_cnb,
                billed_not_charged_count=n_bnc,
                total_charged_amount=prop_total_charged,
                total_billed_amount=prop_total_billed,
            )
        )

        total_charged += n_charged
        total_billed += n_billed
        total_matched += matched_count
        total_cnb += n_cnb
        total_bnc += n_bnc

    # Add summaries for unmatched properties (charges side)
    for norm_prop in sorted(charge_prop_keys - boom_prop_keys):
        prop_charges = charges_by_prop[norm_prop]
        display_name = prop_charges[0].property_name
        charged_with_amount = [r for r in prop_charges if r.amount > 0]
        n_charged = len(charged_with_amount)
        prop_total = sum(r.amount for r in charged_with_amount)

        for rec in charged_with_amount:
            all_discrepancies.append(
                ResidentDiscrepancy(
                    discrepancy_type="charged_not_billed",
                    property_name=display_name,
                    unit=rec.unit,
                    resident_name=rec.resident_name,
                    lease_status=rec.lease_status,
                    charged_amount=rec.amount,
                    billed_amount=Decimal("0"),
                )
            )

        property_summaries.append(
            PropertyChargesummary(
                property_name=display_name,
                residents_charged=n_charged,
                residents_billed=0,
                matched_count=0,
                charged_not_billed_count=n_charged,
                billed_not_charged_count=0,
                total_charged_amount=prop_total,
                total_billed_amount=Decimal("0"),
            )
        )
        total_charged += n_charged
        total_cnb += n_charged

    # Add summaries for unmatched properties (Boom side)
    for norm_prop in sorted(boom_prop_keys - charge_prop_keys):
        prop_boom = boom_by_prop[norm_prop]
        display_name = prop_boom[0].property_name
        n_billed = len(set(r.normalized_name for r in prop_boom))
        prop_total = sum(abs(r.amount) for r in prop_boom)

        for rec in prop_boom:
            all_discrepancies.append(
                ResidentDiscrepancy(
                    discrepancy_type="billed_not_charged",
                    property_name=display_name,
                    unit=rec.unit,
                    resident_name=rec.resident_name,
                    lease_status="",
                    charged_amount=Decimal("0"),
                    billed_amount=abs(rec.amount),
                )
            )

        property_summaries.append(
            PropertyChargesummary(
                property_name=display_name,
                residents_charged=0,
                residents_billed=n_billed,
                matched_count=0,
                charged_not_billed_count=0,
                billed_not_charged_count=len(prop_boom),
                total_charged_amount=Decimal("0"),
                total_billed_amount=prop_total,
            )
        )
        total_billed += n_billed
        total_bnc += len(prop_boom)

    # Sort summaries alphabetically
    property_summaries.sort(key=lambda s: s.property_name)

    return ResidentChargesReconciliationResult(
        run_id=effective_run_id,
        reporting_month=reporting_month,
        vendor=vendor,
        charges_file_name=charges_file_name or Path(charges_file_path).name,
        invoice_file_name=invoice_file_name or Path(invoice_file_path).name,
        property_summaries=property_summaries,
        discrepancies=all_discrepancies,
        unmatched_charge_properties=unmatched_charge_props,
        unmatched_invoice_properties=unmatched_boom_props,
        total_residents_charged=total_charged,
        total_residents_billed=total_billed,
        total_matched=total_matched,
        total_charged_not_billed=total_cnb,
        total_billed_not_charged=total_bnc,
    )
