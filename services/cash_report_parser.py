"""
Entrata cash-received report parser.

Supports CSV and XLSX.  Column headings in Entrata exports may change between
report versions; use ``ENTRATA_COLUMN_MAP`` (or pass a custom map) so that
minor heading variations do not break parsing.

Usage::

    from services.cash_report_parser import parse_cash_report

    records = parse_cash_report(
        file_path="Receipts by Charge Code for Rent Plus.csv",
        reporting_month="2026-07",
        charge_code="RENTPLUS",
    )
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from models.reconciliation_models import CashRecord
from services.utils import normalize_text, parse_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default column mappings (first match wins, case-insensitive)
# ---------------------------------------------------------------------------

#: Override at import time or pass ``column_map`` to ``parse_cash_report``.
ENTRATA_COLUMN_MAP: Dict[str, List[str]] = {
    "property_name": [
        "property", "property name", "property_name", "unit", "community",
    ],
    "pms_property_id": [
        "property id", "property_id", "property code", "prop id",
        "pms_property_id", "unit id",
    ],
    "charge_code": [
        "charge code", "charge_code", "ar code", "ar_code",
        "code", "type",
    ],
    "cash_received": [
        "cash collected", "total cash collections", "cash received",
        "cash_received", "receipts", "collected", "payments",
    ],
    "adjustment_amount": [
        "adjustment", "adjustment amount", "adjustments", "adj",
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_cash_report(
    file_path: str,
    reporting_month: str,
    *,
    charge_code: Optional[str] = None,
    column_map: Optional[Dict[str, List[str]]] = None,
) -> List[CashRecord]:
    """Parse an Entrata cash-received report.

    Multiple rows for the same property are aggregated into a single
    ``CashRecord`` (cash_received summed, adjustment_amount summed).

    Args:
        file_path:       Absolute path to the file (CSV or XLSX).
        reporting_month: YYYY-MM string.  All returned records carry this
                         month; no per-row date filtering is applied in
                         Phase 1 (the user is expected to upload the report
                         for the correct period).
        charge_code:     If provided, only rows matching this charge code are
                         kept.  Pass ``None`` to include all rows.
        column_map:      Optional override for column-name resolution.

    Returns:
        A list of ``CashRecord`` objects, one per unique property.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError:        If the file type is unsupported or required columns
                           are missing.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Cash report file not found: {file_path}")

    ext = path.suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise ValueError(
            f"Unsupported cash report file type: {ext!r}.  "
            "Allowed types: .csv, .xlsx"
        )

    file_hash = _hash_file(file_path)
    logger.info("Parsing cash report: %s  hash=%s", path.name, file_hash)

    effective_map = {**ENTRATA_COLUMN_MAP, **(column_map or {})}

    df = _read_file(path)
    df = _resolve_columns(df, effective_map)
    df = _clean_dataframe(df)

    if charge_code:
        before = len(df)
        df = df[
            df["charge_code"].str.strip().str.upper()
            == charge_code.strip().upper()
        ].copy()
        logger.info(
            "Filtered by charge_code=%r: %d → %d rows", charge_code, before, len(df)
        )

    records = _aggregate_records(df, reporting_month, path.name)

    logger.info(
        "Cash report parsed: %d property records for %s", len(records), reporting_month
    )
    return records


def hash_cash_report(file_path: str) -> str:
    """Return SHA-256 hex digest of a cash report file."""
    return _hash_file(file_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_file(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_file(path: Path) -> pd.DataFrame:
    """Read a cash report file, auto-detecting the real header row.

    Some Entrata exports include a title / date-range block above the
    actual column headers (especially XLSX).  Read without assuming a
    header row, then locate the row that best matches known column-name
    candidates before splitting it out as the header.
    """
    if path.suffix.lower() == ".csv":
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, header=None)
    else:
        raw = pd.read_excel(path, dtype=str, keep_default_na=False, header=None)

    header_row = _find_header_row(raw)
    df = raw.iloc[header_row + 1:].reset_index(drop=True)
    df.columns = [str(v) for v in raw.iloc[header_row].tolist()]
    return df


_MAX_HEADER_SCAN_ROWS = 15


def _find_header_row(raw: pd.DataFrame) -> int:
    """Return the index of the row most likely to be the real header row.

    Scans the first ``_MAX_HEADER_SCAN_ROWS`` rows and picks the one whose
    cell values best match known Entrata column-name candidates.  Falls back
    to row 0 (previous behaviour) if nothing matches.
    """
    candidates = {
        c.lower().strip()
        for values in ENTRATA_COLUMN_MAP.values()
        for c in values
    }
    best_row = 0
    best_score = 0
    scan_limit = min(_MAX_HEADER_SCAN_ROWS, len(raw))
    for i in range(scan_limit):
        row_values = [str(v).lower().strip() for v in raw.iloc[i].tolist()]
        score = sum(1 for v in row_values if v in candidates)
        if score > best_score:
            best_score = score
            best_row = i
    return best_row


def _resolve_columns(
    df: pd.DataFrame,
    column_map: Dict[str, List[str]],
) -> pd.DataFrame:
    """Map source headings to canonical names."""
    lower_to_original = {c.lower().strip(): c for c in df.columns}
    rename_map: Dict[str, str] = {}

    for canonical, candidates in column_map.items():
        if canonical in df.columns:
            continue
        for candidate in candidates:
            source_col = lower_to_original.get(candidate.lower().strip())
            if source_col:
                rename_map[source_col] = canonical
                break

    df = df.rename(columns=rename_map)

    required = {"property_name", "cash_received"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Cash report is missing required columns: {sorted(missing)}.  "
            f"Available columns: {list(df.columns)}"
        )

    # Supply optional columns with defaults if absent
    if "pms_property_id" not in df.columns:
        df["pms_property_id"] = ""
    if "charge_code" not in df.columns:
        df["charge_code"] = ""
    if "adjustment_amount" not in df.columns:
        df["adjustment_amount"] = "0"

    return df


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully-empty rows."""
    df = df.dropna(how="all").copy()
    df = df[df["property_name"].str.strip() != ""].copy()
    return df.reset_index(drop=True)


def _aggregate_records(
    df: pd.DataFrame,
    reporting_month: str,
    source_file_name: str,
) -> List[CashRecord]:
    """Aggregate multiple rows per property into one CashRecord each."""
    from decimal import Decimal

    # Build per-row records
    rows: List[dict] = []
    for _, row in df.iterrows():
        prop_name = str(row.get("property_name", "")).strip()
        pms_id = str(row.get("pms_property_id", "")).strip()
        charge = str(row.get("charge_code", "")).strip()
        try:
            cash = parse_decimal(row.get("cash_received", "0"))
        except ValueError:
            cash = Decimal("0")
        try:
            adj = parse_decimal(row.get("adjustment_amount", "0"))
        except ValueError:
            adj = Decimal("0")

        rows.append(
            {
                "pms_property_id": pms_id,
                "property_name": prop_name,
                "normalized_property_name": normalize_text(prop_name),
                "charge_code": charge,
                "cash_received": cash,
                "adjustment_amount": adj,
            }
        )

    # Aggregate by (pms_property_id, normalized_property_name)
    aggregated: Dict[str, dict] = {}
    for r in rows:
        key = (r["pms_property_id"], r["normalized_property_name"])
        if key not in aggregated:
            aggregated[key] = dict(r)
        else:
            aggregated[key]["cash_received"] += r["cash_received"]
            aggregated[key]["adjustment_amount"] += r["adjustment_amount"]

    records: List[CashRecord] = []
    for r in aggregated.values():
        records.append(
            CashRecord(
                reporting_month=reporting_month,
                pms_property_id=r["pms_property_id"],
                property_name=r["property_name"],
                normalized_property_name=r["normalized_property_name"],
                charge_code=r["charge_code"],
                cash_received=r["cash_received"],
                adjustment_amount=r["adjustment_amount"],
                source_file_name=source_file_name,
            )
        )
    return records
