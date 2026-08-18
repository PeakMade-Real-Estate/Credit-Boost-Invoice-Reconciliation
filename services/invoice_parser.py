"""
Vendor invoice parser.

Supports CSV and XLSX formats.  PDF parsing is intentionally left as a clearly
marked placeholder because PDF extraction varies significantly by vendor; do
not build the system around it in Phase 1.

Column names in the source file are resolved via configurable mappings so that
minor heading variations do not break parsing.

Usage::

    from services.invoice_parser import parse_invoice

    lines, source_total, metadata = parse_invoice(
        file_path="invoice.xlsx",
        vendor="Rent Plus",
        reporting_month="2026-07",
    )
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models.reconciliation_models import InvoiceLine, InvoiceSummary, MatchStatus
from services.utils import normalize_text, parse_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column-name mappings (first match wins, case-insensitive)
# ---------------------------------------------------------------------------

#: Override these at import time or pass *column_map* to ``parse_invoice``.
DEFAULT_COLUMN_MAP: Dict[str, List[str]] = {
    "vendor_property_id": [
        "property_id", "prop_id", "property code", "vendor property id",
        "vendor_property_id", "propid",
    ],
    "original_description": [
        "description", "service description", "item description",
        "line description", "property description", "property_description",
        "property name", "property",
    ],
    "original_quantity": [
        "quantity", "qty", "units", "count", "enrolled",
    ],
    "unit_price": [
        "unit price", "rate", "price", "per unit", "unit_price", "unitprice",
    ],
    "line_amount": [
        "amount", "total", "line amount", "extended amount", "line_amount",
        "net amount", "charge",
    ],
}

#: Column or value patterns that indicate a subtotal / grand-total row.
#: These rows are parsed separately as the source_invoice_total.
TOTAL_ROW_INDICATORS: List[str] = [
    "total", "grand total", "invoice total", "subtotal", "sum",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_invoice(
    file_path: str,
    vendor: str,
    reporting_month: str,
    *,
    column_map: Optional[Dict[str, List[str]]] = None,
    source_invoice_total: Optional[str] = None,
) -> Tuple[List[InvoiceLine], InvoiceSummary]:
    """Parse a vendor invoice file and return normalised line items.

    Args:
        file_path:            Absolute path to the invoice file (CSV or XLSX).
        vendor:               Vendor name (e.g. ``"Rent Plus"``).
        reporting_month:      YYYY-MM string (e.g. ``"2026-07"``).
        column_map:           Optional override for column-name resolution.
        source_invoice_total: If the user entered the invoice total manually,
                              pass it here as a string; otherwise it is read
                              from a total row in the file.

    Returns:
        A tuple of (invoice_lines, invoice_summary).

    Raises:
        ValueError:  If the file extension is unsupported or required columns
                     are missing.
        FileNotFoundError: If *file_path* does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Invoice file not found: {file_path}")

    ext = path.suffix.lower()
    if ext == ".pdf":
        # Delegate to the vendor-specific PDF parser.
        # The PDF parser returns the same (lines, summary) tuple.
        from services.pdf_invoice_parser import parse_rent_plus_pdf, get_pdf_parser
        logger.info("Routing %s to PDF parser for vendor=%r", path.name, vendor)
        return parse_rent_plus_pdf(file_path, reporting_month)
    if ext not in (".csv", ".xlsx"):
        raise ValueError(
            f"Unsupported invoice file type: {ext!r}.  "
            "Allowed types: .csv, .xlsx, .pdf"
        )

    file_hash = _hash_file(file_path)
    logger.info("Parsing invoice: %s  hash=%s", path.name, file_hash)

    effective_map = {**DEFAULT_COLUMN_MAP, **(column_map or {})}

    raw_df = _read_file(path)
    df, source_total_from_file = _split_total_row(raw_df)
    df = _resolve_columns(df, effective_map)
    df = _clean_dataframe(df)

    invoice_id = _generate_invoice_id(vendor, reporting_month)
    lines = _build_invoice_lines(df, invoice_id, vendor, reporting_month)

    # Determine source invoice total
    if source_invoice_total is not None:
        try:
            parsed_source_total = parse_decimal(source_invoice_total)
        except ValueError:
            logger.warning(
                "Could not parse manually entered source_invoice_total=%r; "
                "falling back to file total row.",
                source_invoice_total,
            )
            parsed_source_total = source_total_from_file
    else:
        parsed_source_total = source_total_from_file

    calculated_total = sum(ln.line_amount for ln in lines)
    from decimal import Decimal
    if parsed_source_total is None:
        parsed_source_total = calculated_total  # no total row present
    diff = calculated_total - parsed_source_total
    validation_status = "passed" if abs(diff) < Decimal("0.02") else "failed"

    summary = InvoiceSummary(
        invoice_id=invoice_id,
        vendor=vendor,
        reporting_month=reporting_month,
        source_invoice_total=parsed_source_total,
        calculated_invoice_total=calculated_total,
        invoice_difference=diff,
        invoice_validation_status=validation_status,
        line_count=len(lines),
        property_count=len({ln.vendor_property_id for ln in lines if ln.vendor_property_id}),
        source_file_name=path.name,
        source_file_hash=file_hash,
    )

    logger.info(
        "Invoice parsed: %d lines, %d properties, total=%s, status=%s",
        len(lines),
        summary.property_count,
        parsed_source_total,
        validation_status,
    )
    return lines, summary


# ---------------------------------------------------------------------------
# PDF placeholder
# ---------------------------------------------------------------------------


def parse_invoice_pdf(
    file_path: str,
    vendor: str,
    reporting_month: str,
) -> Tuple[List[InvoiceLine], InvoiceSummary]:
    """PDF invoice parser entry point.

    Delegates to the vendor-specific parser registered in
    ``services.pdf_invoice_parser``.  Currently implemented for:

    - **Rent Plus** (``services/pdf_invoice_parser.py``)

    To add a new vendor's PDF format, subclass ``BasePDFInvoiceParser``
    in ``pdf_invoice_parser.py`` and register it in ``_VENDOR_PDF_PARSERS``.

    Args:
        file_path:       Absolute path to the PDF invoice.
        vendor:          Vendor name (selects the correct parser).
        reporting_month: YYYY-MM string.

    Returns:
        Tuple of ``(invoice_lines, invoice_summary)``.
    """
    from services.pdf_invoice_parser import get_pdf_parser, parse_rent_plus_pdf

    vendor_key = vendor.strip().lower()
    if vendor_key == "rent plus":
        return parse_rent_plus_pdf(file_path, reporting_month)

    raise NotImplementedError(
        f"No PDF parser is implemented for vendor {vendor!r}.  "
        "Please export the invoice as CSV or XLSX, or implement a "
        "vendor-specific PDF parser in services/pdf_invoice_parser.py."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_file(file_path: str) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_file(path: Path) -> pd.DataFrame:
    """Read CSV or XLSX into a DataFrame with all columns as strings."""
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.read_excel(path, dtype=str, keep_default_na=False)


def _split_total_row(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, object]:
    """Detect and remove any grand-total row from the DataFrame.

    Returns the cleaned DataFrame and the parsed total value (or None).
    """
    from decimal import Decimal

    total_value = None
    drop_indices = []

    for idx, row in df.iterrows():
        row_values = [str(v).strip().lower() for v in row.values if str(v).strip()]
        is_total = any(
            indicator in cell
            for cell in row_values
            for indicator in TOTAL_ROW_INDICATORS
        )
        if is_total:
            # Try to find a numeric value in this row
            for v in reversed(row.values):
                try:
                    total_value = parse_decimal(v)
                    break
                except (ValueError, TypeError):
                    continue
            drop_indices.append(idx)

    cleaned = df.drop(index=drop_indices).reset_index(drop=True)
    return cleaned, total_value


def _resolve_columns(
    df: pd.DataFrame,
    column_map: Dict[str, List[str]],
) -> pd.DataFrame:
    """Rename source columns to canonical names using the column map.

    Raises:
        ValueError: If a required canonical column cannot be found.
    """
    # Build a lower-case lookup: lowercase_source_col → original_source_col
    lower_to_original = {c.lower().strip(): c for c in df.columns}
    rename_map: Dict[str, str] = {}

    for canonical, candidates in column_map.items():
        if canonical in df.columns:
            continue  # already present
        for candidate in candidates:
            source_col = lower_to_original.get(candidate.lower().strip())
            if source_col:
                rename_map[source_col] = canonical
                break

    df = df.rename(columns=rename_map)

    required = {"original_description", "original_quantity", "unit_price", "line_amount"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Invoice file is missing required columns: {sorted(missing)}.  "
            f"Available columns: {list(df.columns)}"
        )

    if "vendor_property_id" not in df.columns:
        df["vendor_property_id"] = ""

    return df


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully-empty rows and apply text normalisation."""
    df = df.dropna(how="all").copy()
    # Drop rows where key numeric columns are empty
    df = df[df["line_amount"].str.strip() != ""].copy().reset_index(drop=True)
    return df


def _build_invoice_lines(
    df: pd.DataFrame,
    invoice_id: str,
    vendor: str,
    reporting_month: str,
) -> List[InvoiceLine]:
    """Convert a cleaned DataFrame into InvoiceLine dataclass instances."""
    lines: List[InvoiceLine] = []
    for row_idx, row in df.iterrows():
        original_desc = str(row.get("original_description", "")).strip()
        vendor_pid = str(row.get("vendor_property_id", "")).strip()

        try:
            qty = parse_decimal(row.get("original_quantity", "0"))
            price = parse_decimal(row.get("unit_price", "0"))
            amount = parse_decimal(row.get("line_amount", "0"))
        except ValueError as exc:
            logger.warning(
                "Row %d: could not parse numeric value – %s.  Skipping.",
                row_idx + 2,  # +2: 1-based + header row
                exc,
            )
            continue

        line = InvoiceLine(
            invoice_id=invoice_id,
            reporting_month=reporting_month,
            vendor=vendor,
            source_row=int(row_idx) + 2,
            original_description=original_desc,
            normalized_property_name=normalize_text(original_desc),
            vendor_property_id=vendor_pid,
            original_quantity=qty,
            unit_price=price,
            line_amount=amount,
            match_status=MatchStatus.UNMATCHED,
        )
        lines.append(line)
    return lines


def _generate_invoice_id(vendor: str, reporting_month: str) -> str:
    """Generate a unique invoice processing ID."""
    vendor_slug = vendor.upper().replace(" ", "")[:8]
    month_slug = reporting_month.replace("-", "")
    short_uid = str(uuid.uuid4()).split("-")[0].upper()
    return f"INV-{month_slug}-{vendor_slug}-{short_uid}"
