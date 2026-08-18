"""
Shared text and numeric utilities used across all service modules.

These helpers are intentionally stateless pure functions with no framework
dependencies.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------


def normalize_text(value: str) -> str:
    """Return a cleaned, lower-cased version of *value* for comparison.

    Transformations applied (order matters):
      1. Strip leading/trailing whitespace.
      2. Remove carriage returns and newlines.
      3. Collapse repeated internal whitespace to a single space.
      4. Lower-case the result.

    The *original* value is never modified – callers must store the original
    separately before calling this function.

    Args:
        value: Raw string from a source file or user input.

    Returns:
        Normalised string suitable for matching comparisons.
    """
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.lower()


def normalize_unicode(value: str) -> str:
    """Convert unicode characters to their ASCII equivalents where possible."""
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


# ---------------------------------------------------------------------------
# Numeric / currency helpers
# ---------------------------------------------------------------------------


def parse_decimal(value: object, *, default: Optional[Decimal] = None) -> Decimal:
    """Convert *value* to a ``Decimal``, handling common currency formats.

    Transformations:
      - Strips leading/trailing whitespace.
      - Removes dollar signs.
      - Converts ``(123.45)`` accounting notation to ``-123.45``.
      - Treats blank strings as zero (or *default* if provided).

    Raises:
        ValueError: If the value cannot be parsed and *default* is ``None``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return default if default is not None else Decimal("0")
    text = text.replace("$", "").replace(",", "").strip()
    # Accounting negative: (123.45) → -123.45
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        if default is not None:
            return default
        raise ValueError(f"Cannot convert {value!r} to Decimal") from exc


def safe_divide(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    """Return numerator / denominator, or ``None`` if denominator is zero."""
    if denominator == Decimal("0"):
        return None
    return numerator / denominator


# ---------------------------------------------------------------------------
# Reporting month helpers
# ---------------------------------------------------------------------------


def validate_reporting_month(value: str) -> bool:
    """Return True if *value* is a valid YYYY-MM string."""
    return bool(re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value.strip()))


def normalize_reporting_month(value: str) -> Optional[str]:
    """Attempt to parse common month formats and return YYYY-MM.

    Accepts: ``2026-07``, ``2026/07``, ``07/2026``, ``July 2026``, etc.
    Returns ``None`` if the format is unrecognised.
    """
    import calendar

    value = value.strip()
    # YYYY-MM or YYYY/MM
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    # MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", value)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    # Month YYYY  e.g. "July 2026"
    month_names = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", value)
    if m:
        month_num = month_names.get(m.group(1).lower())
        if month_num:
            return f"{m.group(2)}-{month_num:02d}"
    return None
