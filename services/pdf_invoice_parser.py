"""
PDF invoice parser for Rent Plus (and extensible to other vendors).

The Rent Plus PDF invoice layout:
  - Pages 1-N:  6-column line-item table
                ITEM # | DESCRIPTION | UNIT | QTY | UNIT PRICE | AMOUNT
  - Description format:  "<Property Name> - Rent Plus Services"
                         (cell may contain embedded newlines)
  - Last data page:      Aging / outstanding-balances section (7+ columns --
                         automatically excluded by the 6-column filter)
  - Remittance page:     Plain text containing "AMOUNT DUE: $X,XXX.XX"

No explicit vendor property ID is present in the PDF; matching relies on
the normalised property name extracted from the description column.

Key public functions
---------------------
``normalize_rentplus_description(value)``
    Collapse all whitespace; canonicalise the service suffix to
    " - Rent Plus Services".

``extract_rentplus_property_name(description)``
    Normalise then strip the service suffix and trailing vendor annotations
    (e.g. "(new)").  Raises ``ValueError`` if the suffix cannot be found.

``normalize_property_name_for_matching(value)``
    Further normalise a display property name for use as a matching key:
    lowercase, unicode-normalised, punctuation stripped, apostrophes
    straightened.

Vendor-specific adapter pattern
---------------------------------
Each vendor whose PDF format differs should have its own parser class that
subclasses ``BasePDFInvoiceParser``.  Register it in ``_VENDOR_PDF_PARSERS``.

Usage::

    from services.pdf_invoice_parser import parse_rent_plus_pdf

    lines, summary = parse_rent_plus_pdf(
        file_path="Test Files/May 2026.pdf",
        reporting_month="2026-05",
    )
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from models.reconciliation_models import InvoiceLine, InvoiceSummary, MatchStatus
from services.utils import normalize_text, parse_decimal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Number of columns in a valid Rent Plus line-item table.
_LINE_ITEM_COLS = 6

#: Header cell values that indicate a header row (case-insensitive).
_HEADER_CELLS = {"item #", "item#", "item", "description", "qty", "quantity"}

#: Regex to find the invoice total on the remittance page.
_AMOUNT_DUE_RE = re.compile(
    r"AMOUNT\s+DUE\s*[:\s]+\$?\s*([\d,]+\.?\d*)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# RentPlus description parsing constants
# ---------------------------------------------------------------------------

#: Anchored pattern that matches the canonical "- Rent Plus Services" suffix
#: at the end of a *normalised* description string.
RENTPLUS_SERVICE_SUFFIX_PATTERN = re.compile(
    r"\s*-\s*Rent\s+Plus\s+Services\s*$",
    flags=re.IGNORECASE,
)

#: Trailing vendor-only annotations to strip from the property name after
#: removing the service suffix.  Applied in order.
RENTPLUS_TRAILING_PROPERTY_ANNOTATIONS: list[re.Pattern] = [
    re.compile(r"\s*\(new\)\s*$", flags=re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Public description-parsing functions
# ---------------------------------------------------------------------------


def normalize_rentplus_description(value: str) -> str:
    """Normalise a raw RentPlus DESCRIPTION cell value.

    Steps:
    1. Convert to str safely (handles None).
    2. Collapse all whitespace sequences (including newlines, tabs) to a
       single space and trim.
    3. Canonicalise variations of the service suffix so that spacing around
       the hyphen and within "Rent Plus Services" is always consistent.

    Examples::

        >>> normalize_rentplus_description("Campus Creek Cottages - Rent Plus\\nServices")
        'Campus Creek Cottages - Rent Plus Services'
        >>> normalize_rentplus_description("Campus Creek Cottages- Rent Plus Services")
        'Campus Creek Cottages - Rent Plus Services'
        >>> normalize_rentplus_description("  Station 42  -  Rent Plus  Services  ")
        'Station 42 - Rent Plus Services'
    """
    if value is None:
        return ""

    # Collapse all whitespace (including \\n \\r \\t) to a single space
    normalized = re.sub(r"\s+", " ", str(value)).strip()

    # Canonicalise the service suffix to exactly " - Rent Plus Services"
    normalized = re.sub(
        r"\s*-\s*Rent\s+Plus\s+Services\s*$",
        " - Rent Plus Services",
        normalized,
        flags=re.IGNORECASE,
    )

    return normalized


def extract_rentplus_property_name(description: str) -> str:
    """Extract the property display name from a RentPlus description.

    The algorithm:
    1. Normalise the description with ``normalize_rentplus_description``.
    2. Locate the final " - Rent Plus Services" suffix.
    3. Return everything before it, after removing any trailing vendor-only
       annotations (e.g. ``(new)``).

    Args:
        description: Raw or already-normalised DESCRIPTION cell value.

    Returns:
        Property display name, e.g. ``"Campus Creek Cottages"``.

    Raises:
        ValueError: If the description is empty or the suffix cannot be found.

    Examples::

        >>> extract_rentplus_property_name("48 West - Rent Plus Services")
        '48 West'
        >>> extract_rentplus_property_name(
        ...     "The Summit at Coates Run (new) - Rent Plus Services"
        ... )
        'The Summit at Coates Run'
        >>> extract_rentplus_property_name(
        ...     "Campus Creek Cottages - Rent Plus\\nServices"
        ... )
        'Campus Creek Cottages'
    """
    normalized = normalize_rentplus_description(description)

    if not normalized:
        raise ValueError("RentPlus description is empty.")

    match = RENTPLUS_SERVICE_SUFFIX_PATTERN.search(normalized)
    if not match:
        raise ValueError(
            f"Unable to identify RentPlus service suffix in description: "
            f"{description!r}"
        )

    property_name = normalized[: match.start()].strip()

    # Remove trailing vendor-only annotations (e.g. "(new)")
    for annotation_pattern in RENTPLUS_TRAILING_PROPERTY_ANNOTATIONS:
        property_name = annotation_pattern.sub("", property_name).strip()

    if not property_name:
        raise ValueError(
            f"Property name was empty after parsing description: {description!r}"
        )

    return property_name


def normalize_property_name_for_matching(value: str) -> str:
    """Normalise a property display name for use as a matching key.

    Steps:
    1. NFKC unicode normalisation.
    2. Curly apostrophes → straight apostrophe.
    3. Collapse whitespace and lower-case.
    4. Remove characters that are not word characters, spaces, apostrophes,
       or ampersands.

    Business-significant words (The, Apartments, etc.) are **never** removed.
    Numbers and letters are always preserved.

    Examples::

        >>> normalize_property_name_for_matching("The Summit at Coates Run")
        'the summit at coates run'
        >>> normalize_property_name_for_matching("Hannah Townhomes & Lofts")
        'hannah townhomes & lofts'
        >>> normalize_property_name_for_matching("The Finmore at 241")
        'the finmore at 241'
        >>> normalize_property_name_for_matching("i5 Wynwood")
        'i5 wynwood'
    """
    if value is None:
        return ""

    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = normalized.replace("\u2019", "'").replace("\u2018", "'")  # curly apostrophes
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    normalized = re.sub(r"[^\w\s'&]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_rent_plus_pdf(
    file_path: str,
    reporting_month: str,
) -> Tuple[List[InvoiceLine], InvoiceSummary]:
    """Parse a Rent Plus PDF invoice.

    Args:
        file_path:       Absolute path to the PDF file.
        reporting_month: YYYY-MM string (e.g. ``"2026-05"``).

    Returns:
        Tuple of ``(invoice_lines, invoice_summary)``.

    Raises:
        FileNotFoundError: If the file does not exist.
        ImportError:       If ``pdfplumber`` is not installed.
        ValueError:        If no line-item tables are found.
    """
    _require_pdfplumber()
    import pdfplumber  # noqa: PLC0415

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF invoice not found: {file_path}")

    file_hash = _hash_file(file_path)
    vendor = "Rent Plus"
    logger.info("Parsing PDF invoice: %s  hash=%s", path.name, file_hash)

    with pdfplumber.open(file_path) as pdf:
        raw_rows = _extract_line_item_rows(pdf)
        source_total = _extract_invoice_total(pdf)

    if not raw_rows:
        raise ValueError(
            f"No line-item rows were found in {path.name}.  "
            "Ensure the file is a Rent Plus invoice in the expected format."
        )

    invoice_id = _generate_invoice_id(vendor, reporting_month)
    lines = _build_invoice_lines(raw_rows, invoice_id, vendor, reporting_month)

    calculated_total = sum(ln.line_amount for ln in lines)
    if source_total is None:
        source_total = calculated_total

    diff = calculated_total - source_total
    val_status = "passed" if abs(diff) <= Decimal("0.02") else "failed"

    summary = InvoiceSummary(
        invoice_id=invoice_id,
        vendor=vendor,
        reporting_month=reporting_month,
        source_invoice_total=source_total,
        calculated_invoice_total=calculated_total,
        invoice_difference=diff,
        invoice_validation_status=val_status,
        line_count=len(lines),
        property_count=len(
            {ln.normalized_property_name for ln in lines if ln.normalized_property_name}
        ),
        source_file_name=path.name,
        source_file_hash=file_hash,
    )

    logger.info(
        "PDF parsed: %d lines, %d properties, total=%s, status=%s",
        len(lines),
        summary.property_count,
        source_total,
        val_status,
    )
    return lines, summary


# ---------------------------------------------------------------------------
# Extensible base (for future non-Rent-Plus PDF formats)
# ---------------------------------------------------------------------------


class BasePDFInvoiceParser:
    """Base class for vendor-specific PDF invoice parsers.

    To support a new vendor PDF layout, subclass this and override
    ``extract_property_name()`` (and optionally ``parse_description()``).
    Register the subclass in ``_VENDOR_PDF_PARSERS``.
    """

    vendor: str = ""
    line_item_cols: int = 6

    def extract_property_name(self, description: str) -> str:
        """Return the property name from the description cell.

        Override in vendor-specific subclasses.
        """
        return normalize_text(description.replace("\n", " ").strip())

    def parse_description(self, description: str) -> dict:
        """Parse description and return a structured result dict.

        Keys: ``normalized_description``, ``extracted_property_name``,
        ``normalized_property_name``, ``property_parse_status``,
        ``property_parse_exception``.
        """
        name = self.extract_property_name(description)
        return {
            "normalized_description": re.sub(r"\s+", " ", description).strip(),
            "extracted_property_name": name,
            "normalized_property_name": normalize_text(name),
            "property_parse_status": "parsed",
            "property_parse_exception": None,
        }

    def is_line_item_row(self, row: list) -> bool:
        """Return True if *row* is a billable line-item (not a header/footer)."""
        if not row or len(row) < self.line_item_cols:
            return False
        item_cell = str(row[0] or "").strip()
        if not item_cell:
            return False
        if item_cell.lower() in _HEADER_CELLS:
            return False
        try:
            int(item_cell)
        except ValueError:
            return False
        amount_cell = str(row[-1] or "").strip()
        return bool(amount_cell)


class RentPlusPDFParser(BasePDFInvoiceParser):
    """Rent Plus invoice PDF parser.

    Uses ``extract_rentplus_property_name()`` to strip the
    " - Rent Plus Services" suffix and any trailing vendor annotations.
    This logic is intentionally RentPlus-specific and is never applied to
    other vendor profiles.
    """

    vendor = "Rent Plus"

    def extract_property_name(self, description: str) -> str:
        """Extract property name, falling back to description if suffix missing."""
        try:
            return extract_rentplus_property_name(description)
        except ValueError:
            # Graceful fallback: return normalised description as-is
            return normalize_rentplus_description(description)

    def parse_description(self, description: str) -> dict:
        """Return structured parse result including audit fields.

        On success:  ``property_parse_status = "parsed"``
        On failure:  ``property_parse_status = "failed"``,
                     ``property_parse_exception`` contains the error message.
        """
        normalized_desc = normalize_rentplus_description(description)
        try:
            property_name = extract_rentplus_property_name(description)
            return {
                "normalized_description": normalized_desc,
                "extracted_property_name": property_name,
                "normalized_property_name": normalize_property_name_for_matching(property_name),
                "property_parse_status": "parsed",
                "property_parse_exception": None,
            }
        except ValueError as exc:
            logger.warning(
                "RentPlus description parse failure: %s  (original: %r)",
                exc,
                description,
            )
            return {
                "normalized_description": normalized_desc,
                "extracted_property_name": "",
                "normalized_property_name": "",
                "property_parse_status": "failed",
                "property_parse_exception": str(exc),
            }


#: Registry of vendor name (lower-cased) -> parser class.
_VENDOR_PDF_PARSERS: dict[str, type[BasePDFInvoiceParser]] = {
    "rent plus": RentPlusPDFParser,
}


def get_pdf_parser(vendor: str) -> BasePDFInvoiceParser:
    """Return the appropriate PDF parser instance for *vendor*."""
    cls = _VENDOR_PDF_PARSERS.get(vendor.strip().lower())
    if cls is None:
        logger.warning(
            "No vendor-specific PDF parser found for %r; using base parser.",
            vendor,
        )
        return BasePDFInvoiceParser()
    return cls()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_line_item_rows(pdf) -> list[dict]:
    """Walk every page and extract rows from 6-column line-item tables.

    Handles two PDF layout scenarios:

    **Case A – description wraps within a single cell** (most common):
    pdfplumber keeps the text in one cell separated by ``\\n``.  The
    ``normalize_rentplus_description`` function handles the embedded newline.

    **Case B – table row splits across physical PDF rows**:
    pdfplumber returns two separate table rows.  The first has a numeric
    item # but empty qty/price/amount; the second has no item # but the
    continuation of the description and the numeric data.  The parser
    detects this and merges the pair.
    """
    rows: list[dict] = []
    source_row_num = 1
    pending: dict | None = None  # Incomplete row awaiting a continuation row

    for page_num, page in enumerate(pdf.pages, start=1):
        tables = page.extract_tables()
        for table in tables:
            if not table:
                continue
            # Only process tables that have at least one 6-column row
            col_counts = {len(r) for r in table if r}
            if _LINE_ITEM_COLS not in col_counts:
                continue

            for row in table:
                if not row or len(row) != _LINE_ITEM_COLS:
                    continue

                item_cell  = str(row[0] or "").strip()
                desc_cell  = str(row[1] or "").strip()
                unit_cell  = str(row[2] or "").strip()
                qty_cell   = str(row[3] or "").strip()
                price_cell = str(row[4] or "").strip()
                amount_cell = str(row[5] or "").strip()

                # Skip header rows
                if item_cell.lower() in _HEADER_CELLS:
                    continue
                # Skip completely blank rows
                if not any([item_cell, desc_cell, unit_cell, qty_cell,
                             price_cell, amount_cell]):
                    continue

                # Determine whether item # is a valid integer
                try:
                    int(item_cell)
                    item_is_numeric = True
                except ValueError:
                    item_is_numeric = False

                has_amount   = bool(amount_cell)
                has_quantity = bool(qty_cell)

                if item_is_numeric:
                    # Flush any pending incomplete row before starting a new one
                    if pending and pending.get("amount"):
                        rows.append(pending)
                        source_row_num += 1
                    pending = None

                    if has_amount and has_quantity:
                        # Complete row -- store immediately
                        rows.append({
                            "source_row": source_row_num,
                            "page": page_num,
                            "item_num": item_cell,
                            "description": desc_cell,
                            "unit": unit_cell,
                            "quantity": qty_cell,
                            "unit_price": price_cell,
                            "amount": amount_cell,
                        })
                        source_row_num += 1
                    else:
                        # Partial row (Case B start): description wraps to next row
                        pending = {
                            "source_row": source_row_num,
                            "page": page_num,
                            "item_num": item_cell,
                            "description": desc_cell,
                            "unit": unit_cell,
                            "quantity": qty_cell,
                            "unit_price": price_cell,
                            "amount": amount_cell,
                        }

                elif pending:
                    # Continuation row -- append description, fill in missing data
                    if desc_cell:
                        sep = " " if pending["description"] else ""
                        pending["description"] += sep + desc_cell
                    if not pending["unit"] and unit_cell:
                        pending["unit"] = unit_cell
                    if not pending["quantity"] and qty_cell:
                        pending["quantity"] = qty_cell
                    if not pending["unit_price"] and price_cell:
                        pending["unit_price"] = price_cell
                    if not pending["amount"] and amount_cell:
                        pending["amount"] = amount_cell
                    # Once all required fields are present, finalise
                    if pending.get("amount") and pending.get("quantity"):
                        rows.append(pending)
                        source_row_num += 1
                        pending = None

    # Flush any trailing pending row that received all required data
    if pending and pending.get("amount") and pending.get("quantity"):
        rows.append(pending)

    return rows


def _extract_invoice_total(pdf) -> Optional[Decimal]:
    """Scan all pages (in reverse) for an AMOUNT DUE line."""
    for page in reversed(pdf.pages):
        text = page.extract_text() or ""
        match = _AMOUNT_DUE_RE.search(text)
        if match:
            try:
                return parse_decimal(match.group(1))
            except ValueError:
                continue
    return None


def _build_invoice_lines(
    raw_rows: list[dict],
    invoice_id: str,
    vendor: str,
    reporting_month: str,
) -> List[InvoiceLine]:
    """Convert raw row dicts into typed ``InvoiceLine`` objects.

    Uses ``parser.parse_description()`` to populate the description audit
    fields.  Rows where numeric values cannot be parsed are skipped with
    a warning.  Rows where the property name cannot be extracted are
    **retained** with ``property_parse_status = "failed"``; they will appear
    in the reconciliation result as UNMATCHED lines and generate a
    RENTPLUS_PROPERTY_PARSE_FAILURE exception during property matching.
    """
    parser = get_pdf_parser(vendor)
    lines: List[InvoiceLine] = []

    for raw in raw_rows:
        description = raw["description"]
        parsed = parser.parse_description(description)

        try:
            qty   = parse_decimal(raw["quantity"])
            price = parse_decimal(raw["unit_price"])
            amount = parse_decimal(raw["amount"])
        except ValueError as exc:
            logger.warning(
                "PDF row %d: could not parse numeric value -- %s.  Skipping.",
                raw["source_row"],
                exc,
            )
            continue

        lines.append(
            InvoiceLine(
                invoice_id=invoice_id,
                reporting_month=reporting_month,
                vendor=vendor,
                source_row=raw["source_row"],
                original_description=description,
                normalized_property_name=parsed["normalized_property_name"],
                vendor_property_id="",
                original_quantity=qty,
                unit_price=price,
                line_amount=amount,
                match_status=MatchStatus.UNMATCHED,
                # New description-audit fields
                normalized_description=parsed["normalized_description"],
                extracted_property_name=parsed["extracted_property_name"],
                property_parse_status=parsed["property_parse_status"],
                property_parse_exception=parsed["property_parse_exception"],
            )
        )
    return lines


def _hash_file(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _generate_invoice_id(vendor: str, reporting_month: str) -> str:
    vendor_slug = vendor.upper().replace(" ", "")[:8]
    month_slug = reporting_month.replace("-", "")
    short_uid = str(uuid.uuid4()).split("-")[0].upper()
    return f"INV-{month_slug}-{vendor_slug}-{short_uid}"


def _require_pdfplumber() -> None:
    """Raise a clear error if pdfplumber is not installed."""
    try:
        import pdfplumber  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "pdfplumber is required for PDF invoice parsing.  "
            "Install it with:  pip install pdfplumber"
        ) from exc
