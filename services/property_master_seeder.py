"""
Property master seeder.

Produces a list of proposed property master rows by:

1. Extracting the unique property display names from a parsed PDF invoice.
2. Fuzzy-matching each name against the Entrata cash report so that PMS
   property IDs and charge codes can be pre-filled automatically.
3. Auto-assigning suggested internal property IDs (CB-XXXX) starting
   after the highest number already present in the existing property master.

The seeder never writes to disk — the caller (setup_routes) is responsible
for rendering the review page and persisting the approved rows.

Usage::

    from services.property_master_seeder import seed_from_invoice_and_cash_report

    rows = seed_from_invoice_and_cash_report(invoice_lines, cash_records)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from models.reconciliation_models import CashRecord, InvoiceLine, PropertyMaster
from services.pdf_invoice_parser import normalize_property_name_for_matching

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz
    from rapidfuzz import process as rf_process

    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not installed — fuzzy cash-report matching disabled.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_from_invoice_and_cash_report(
    invoice_lines: List[InvoiceLine],
    cash_records: List[CashRecord],
    existing_property_master: Optional[List[PropertyMaster]] = None,
    fuzzy_threshold: int = 70,
    vendor: str = "Rent Plus",
) -> List[dict]:
    """Generate proposed property master rows from invoice + cash report.

    Args:
        invoice_lines:            Parsed invoice lines.  Only
                                  ``extracted_property_name`` is used.
        cash_records:             Parsed Entrata cash records.  Used to
                                  pre-fill ``pms_property_id`` and
                                  ``charge_code``.
        existing_property_master: Current property master — used to mark
                                   rows that already exist (``is_new=False``)
                                   and to determine the next CB-XXXX number.
        fuzzy_threshold:          Minimum rapidfuzz score (0–100) to accept
                                   a fuzzy name match to a cash report record.
        vendor:                   Vendor label to write into each proposed row.

    Returns:
        List of dicts sorted alphabetically by property name.  Each dict
        contains the full set of property master columns plus three
        display-only keys that are *not* written to CSV:
        ``cash_match_name``, ``cash_match_confidence``, ``is_new``.
    """
    existing_pm = existing_property_master or []

    # ------------------------------------------------------------------ #
    # 1. Index the existing property master
    # ------------------------------------------------------------------ #
    existing_normalized: set[str] = set()
    max_cb: int = 0
    for p in existing_pm:
        existing_normalized.add(p.normalized_property_name)
        iid = (p.internal_property_id or "").upper()
        if iid.startswith("CB-"):
            try:
                max_cb = max(max_cb, int(iid[3:]))
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # 2. Collect unique property names from invoice
    # ------------------------------------------------------------------ #
    seen: set[str] = set()
    unique_names: list[str] = []
    for line in invoice_lines:
        name = (line.extracted_property_name or "").strip()
        if not name:
            continue
        key = normalize_property_name_for_matching(name)
        if key not in seen:
            seen.add(key)
            unique_names.append(name)
    unique_names.sort()

    # ------------------------------------------------------------------ #
    # 3. Build cash record lookup (normalised name → CashRecord)
    # ------------------------------------------------------------------ #
    cash_by_norm: Dict[str, CashRecord] = {}
    for rec in cash_records:
        key = normalize_property_name_for_matching(rec.property_name)
        if key:
            cash_by_norm[key] = rec
    cash_norm_names = list(cash_by_norm.keys())

    # ------------------------------------------------------------------ #
    # 4. Build proposed rows
    # ------------------------------------------------------------------ #
    proposed: list[dict] = []
    cb_counter = max_cb + 1

    for invoice_name in unique_names:
        norm = normalize_property_name_for_matching(invoice_name)
        is_new = norm not in existing_normalized

        # Exact match first
        cash_rec: Optional[CashRecord] = cash_by_norm.get(norm)
        confidence = 100.0 if cash_rec else 0.0

        # Fuzzy fallback
        if cash_rec is None and cash_norm_names and _RAPIDFUZZ_AVAILABLE:
            result = rf_process.extractOne(
                norm,
                cash_norm_names,
                scorer=fuzz.token_sort_ratio,
            )
            if result is not None:
                best, score, _ = result
                if score >= fuzzy_threshold:
                    cash_rec = cash_by_norm[best]
                    confidence = float(score)

        row: dict = {
            # --- Property master columns ---
            "internal_property_id":      f"CB-{cb_counter:04d}" if is_new else "",
            "pms_property_id":           cash_rec.pms_property_id if cash_rec else "",
            "vendor_property_id":        "",
            "property_name":             invoice_name,
            "normalized_property_name":  norm,
            "vendor":                    vendor,
            "program_status":            "active",
            "program_start_date":        "",
            "program_end_date":          "",
            "charge_code":               cash_rec.charge_code if cash_rec else "",
            "active":                    "true",
            # --- Display-only (not written to CSV) ---
            "cash_match_name":       cash_rec.property_name if cash_rec else "",
            "cash_match_confidence": confidence,
            "is_new":                is_new,
        }
        proposed.append(row)
        if is_new:
            cb_counter += 1

    new_count = sum(1 for r in proposed if r["is_new"])
    logger.info(
        "Seeder: %d unique invoice properties (%d new, %d already in PM).",
        len(unique_names),
        new_count,
        len(unique_names) - new_count,
    )
    return proposed
