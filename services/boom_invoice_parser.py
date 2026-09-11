"""
Boom export file parser for Credit Boost powered by Boom.

Parses CSV and XLSX transaction export files from the Boom platform.
Returns ``BoomTransactionLine`` objects (not ``InvoiceLine``).

Column headings are resolved via a flexible mapping so that minor heading
variations between Boom export templates do not break parsing.

Usage::

    from services.boom_invoice_parser import parse_boom_file

    lines, summary = parse_boom_file(
        file_path="boom_export.csv",
        reporting_month="2026-07",
    )
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models.boom_models import BoomTransactionLine
from models.reconciliation_models import InvoiceSummary
from services.utils import parse_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default column-name mappings (first match wins, case-insensitive)
# ---------------------------------------------------------------------------

BOOM_COLUMN_MAP: Dict[str, List[str]] = {
    "transaction_id": [
        "transaction id", "transaction_id", "tx id", "txid",
        "transaction number", "tx number", "id",
    ],
    "boom_property_id": [
        "property name", "property_name",
        "boom property id", "boom_property_id",
        "property", "property id", "property_id", "property value",
    ],
    "category": [
        "category", "cat", "transaction category",
    ],
    "transaction_type": [
        "transaction type", "transaction_type", "type", "tx type",
    ],
    "template_name": [
        "template name", "template_name", "template", "program template",
    ],
    "subject_type": [
        "subject type", "subject_type", "subject", "subject category",
    ],
    "transaction_amount": [
        "amount", "transaction amount", "transaction_amount",
        "normalized amount", "normalized_amount",
    ],
}

_VENDOR_NAME = "Credit Boost powered by Boom"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_boom_file(
    file_path: str,
    reporting_month: str,
    *,
    column_map: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[BoomTransactionLine], InvoiceSummary]:
    """Parse a Boom transaction export and return raw lines with a summary.

    Args:
        file_path:       Absolute path to the file (CSV or XLSX).
        reporting_month: YYYY-MM string.
        column_map:      Optional override for column-name resolution.

    Returns:
        Tuple of (lines, invoice_summary).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If the file type is unsupported or required columns
                           are missing.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Boom file not found: {file_path}")

    ext = path.suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise ValueError(
            f"Unsupported Boom file type: {ext!r}.  Expected .csv or .xlsx."
        )

    effective_map = {**BOOM_COLUMN_MAP, **(column_map or {})}

    if ext == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        xlsx = pd.ExcelFile(path)
        sheet_name = "Reconciliation Data" if "Reconciliation Data" in xlsx.sheet_names else 0
        df = pd.read_excel(xlsx, sheet_name=sheet_name, dtype=str, keep_default_na=False)

    # Normalise column names to lowercase + underscores
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    invoice_id = str(uuid.uuid4())

    col_map = _resolve_columns(df.columns.tolist(), effective_map)
    _assert_required_columns(col_map, {"transaction_id", "boom_property_id", "transaction_amount"})

    lines: List[BoomTransactionLine] = []
    for idx, row in df.iterrows():
        lines.append(
            BoomTransactionLine(
                invoice_id=invoice_id,
                reporting_month=reporting_month,
                vendor=_VENDOR_NAME,
                source_row=int(idx) + 2,        # 1-based + header row
                transaction_id=_get(row, col_map, "transaction_id"),
                boom_property_id=_get(row, col_map, "boom_property_id"),
                original_description=_get(row, col_map, "template_name"),
                category=_get(row, col_map, "category"),
                transaction_type=_get(row, col_map, "transaction_type"),
                template_name=_get(row, col_map, "template_name"),
                subject_type=_get(row, col_map, "subject_type"),
                transaction_amount=parse_decimal(
                    row.get(col_map.get("transaction_amount", ""), "0"),
                    default=Decimal("0"),
                ),
            )
        )

    # Source total = sum of all raw amounts (no qualifying filter applied yet)
    calculated_total = sum(
        (l.transaction_amount for l in lines), Decimal("0")
    )

    summary = InvoiceSummary(
        invoice_id=invoice_id,
        vendor=_VENDOR_NAME,
        reporting_month=reporting_month,
        source_invoice_total=calculated_total,
        calculated_invoice_total=calculated_total,
        invoice_difference=Decimal("0"),
        invoice_validation_status="ok",
        line_count=len(lines),
        property_count=len({l.boom_property_id for l in lines}),
        source_file_name=path.name,
        source_file_hash=file_hash,
    )

    logger.info(
        "Parsed Boom file %s: %d lines, %d properties, total=%s",
        path.name,
        len(lines),
        summary.property_count,
        calculated_total,
    )
    return lines, summary


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_columns(
    columns: List[str],
    col_map: Dict[str, List[str]],
) -> Dict[str, str]:
    """Map logical field names to actual (already-normalised) column names."""
    columns_set = set(columns)
    result: Dict[str, str] = {}
    for field, candidates in col_map.items():
        for candidate in candidates:
            norm = candidate.lower().replace(" ", "_")
            if norm in columns_set:
                result[field] = norm
                break
    return result


def _assert_required_columns(col_map: Dict[str, str], required: set) -> None:
    missing = required - set(col_map.keys())
    if missing:
        raise ValueError(
            f"Boom file is missing required columns: {sorted(missing)}.  "
            f"Columns resolved: {sorted(col_map.keys())}"
        )


def _get(row: object, col_map: Dict[str, str], field: str) -> str:
    col = col_map.get(field)
    if col is None:
        return ""
    return str(row.get(col, "")).strip()  # type: ignore[union-attr]
