"""
Rate mapping engine.

Loads configurable rate rules from ``rate_mapping.csv`` and applies them to
each invoice line.  Rules are matched in priority order:

  1. Vendor + Property ID + unit price + effective date  (property-specific)
  2. Vendor + unit price + effective date  (general)

If no rule is found, a BLOCKING exception is raised.

The normalised policy quantity is calculated as::

    normalized_policy_quantity = original_quantity * policy_multiplier * sign

Usage::

    from services.rate_mapping import load_rate_rules, apply_rate_mapping

    rules = load_rate_rules("data/rate_mapping.csv")
    lines, exceptions = apply_rate_mapping(lines, rules, "2026-07")
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models.reconciliation_models import (
    ExceptionSeverity,
    ExceptionType,
    InvoiceLine,
    RateException,
    RateRule,
)
from services.utils import parse_decimal

logger = logging.getLogger(__name__)

# Tolerance when comparing unit prices (handle minor floating-point artefacts)
_PRICE_TOLERANCE = Decimal("0.005")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_rate_rules(file_path: str) -> List[RateRule]:
    """Load rate rules from ``rate_mapping.csv``.

    Args:
        file_path: Absolute path to the rate mapping CSV.

    Returns:
        List of active ``RateRule`` instances.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If required columns are missing.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Rate mapping file not found: {file_path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    required = {
        "rate_rule_id",
        "vendor",
        "invoice_unit_price",
        "rate_name",
        "policy_multiplier",
        "sign",
        "active",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Rate mapping file is missing required columns: {sorted(missing)}"
        )

    rules: List[RateRule] = []
    for _, row in df.iterrows():
        active_raw = str(row.get("active", "false")).strip().lower()
        active = active_raw in ("true", "1", "yes")
        if not active:
            continue  # skip inactive rules

        property_id = str(row.get("property_id", "")).strip() or None

        rules.append(
            RateRule(
                rate_rule_id=str(row["rate_rule_id"]).strip(),
                vendor=str(row["vendor"]).strip(),
                property_id=property_id,
                invoice_unit_price=parse_decimal(row["invoice_unit_price"]),
                rate_name=str(row["rate_name"]).strip(),
                policy_multiplier=parse_decimal(row["policy_multiplier"]),
                sign=parse_decimal(row["sign"]),
                vendor_cost_per_policy=parse_decimal(
                    row.get("vendor_cost_per_policy", "0")
                ),
                peak_share_per_policy=parse_decimal(
                    row.get("peak_share_per_policy", "0")
                ),
                owner_share_per_policy=parse_decimal(
                    row.get("owner_share_per_policy", "0")
                ),
                resident_charge_per_policy=parse_decimal(
                    row.get("resident_charge_per_policy", "0")
                ),
                effective_start_date=_parse_date(row.get("effective_start_date")),
                effective_end_date=_parse_date(row.get("effective_end_date")),
                active=active,
            )
        )

    logger.info(
        "Loaded %d active rate rules from %s", len(rules), path.name
    )
    return rules


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------


def apply_rate_mapping(
    invoice_lines: List[InvoiceLine],
    rate_rules: List[RateRule],
    reporting_month: str,
    *,
    manual_rate_overrides: Optional[Dict[int, str]] = None,
) -> Tuple[List[InvoiceLine], List[RateException]]:
    """Match each invoice line to a rate rule and compute policy quantities.

    Args:
        invoice_lines:        Lines produced by the invoice parser (already
                              property-matched).
        rate_rules:           Rules loaded from ``rate_mapping.csv``.
        reporting_month:      YYYY-MM string used for date-range filtering.
        manual_rate_overrides: Dict mapping source_row → rate_rule_id from the
                              exceptions page.

    Returns:
        Tuple of (updated invoice_lines, list of RateException).
    """
    report_date = _month_to_date(reporting_month)
    overrides = manual_rate_overrides or {}
    rules_by_id: Dict[str, RateRule] = {r.rate_rule_id: r for r in rate_rules}

    exceptions: List[RateException] = []

    for line in invoice_lines:
        rule: Optional[RateRule] = None

        # --- Manual override from exceptions page ---
        if line.source_row in overrides:
            override_rule_id = overrides[line.source_row]
            rule = rules_by_id.get(override_rule_id)
            if rule:
                line.manual_override = True
            else:
                logger.warning(
                    "Row %d: override rate_rule_id=%r not found.",
                    line.source_row,
                    override_rule_id,
                )

        # --- Priority 1: property-specific rule ---
        if rule is None and line.internal_property_id:
            rule = _find_rule(
                rate_rules,
                vendor=line.vendor,
                unit_price=line.unit_price,
                report_date=report_date,
                property_id=line.internal_property_id,
            )

        # --- Priority 2: general rule ---
        if rule is None:
            rule = _find_rule(
                rate_rules,
                vendor=line.vendor,
                unit_price=line.unit_price,
                report_date=report_date,
                property_id=None,
            )

        if rule is None:
            exceptions.append(
                RateException(
                    exception_type=ExceptionType.UNKNOWN_RATE,
                    severity=ExceptionSeverity.BLOCKING,
                    invoice_property_name=line.original_description,
                    invoice_unit_price=line.unit_price,
                    source_row=line.source_row,
                    message=(
                        f"Row {line.source_row}: No rate rule found for "
                        f"vendor={line.vendor!r}, "
                        f"unit_price={line.unit_price}, "
                        f"property={line.internal_property_id or 'N/A'}."
                    ),
                )
            )
            continue

        # Apply rule to line
        _apply_rule(line, rule)

        # Warn about legacy rates
        if "legacy" in rule.rate_name.lower():
            exceptions.append(
                RateException(
                    exception_type=ExceptionType.LEGACY_RATE_ACTIVE,
                    severity=ExceptionSeverity.WARNING,
                    invoice_property_name=line.original_description,
                    invoice_unit_price=line.unit_price,
                    source_row=line.source_row,
                    suggested_rate_rule_id=rule.rate_rule_id,
                    message=(
                        f"Row {line.source_row}: Legacy rate {rule.rate_name!r} "
                        f"is still active for {line.original_description!r}."
                    ),
                )
            )

    logger.info(
        "Rate mapping complete.  %d lines, %d exceptions.",
        len(invoice_lines),
        len(exceptions),
    )
    return invoice_lines, exceptions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_rule(
    rules: List[RateRule],
    vendor: str,
    unit_price: Decimal,
    report_date: Optional[date],
    property_id: Optional[str],
) -> Optional[RateRule]:
    """Search for the best matching rule."""
    candidates = [
        r
        for r in rules
        if (
            r.vendor.strip().lower() == vendor.strip().lower()
            and _prices_match(r.invoice_unit_price, unit_price)
            and _date_in_range(report_date, r.effective_start_date, r.effective_end_date)
            and (
                # Property-specific search: property_id must match
                (property_id is not None and r.property_id == property_id)
                # General search: rule must have no property_id
                or (property_id is None and not r.property_id)
            )
        )
    ]
    if not candidates:
        return None
    # If multiple rules match, prefer the most recently effective one
    candidates.sort(
        key=lambda r: r.effective_start_date or date.min,
        reverse=True,
    )
    return candidates[0]


def _apply_rule(line: InvoiceLine, rule: RateRule) -> None:
    """Stamp rate fields onto a line and calculate policy quantity."""
    line.rate_rule_id = rule.rate_rule_id
    line.rate_type = rule.rate_name
    line.policy_multiplier = rule.policy_multiplier
    line.sign = rule.sign
    line.normalized_policy_quantity = (
        line.original_quantity * rule.policy_multiplier * rule.sign
    )


def _prices_match(rule_price: Decimal, line_price: Decimal) -> bool:
    """Return True if prices are equal within tolerance."""
    return abs(rule_price - line_price) <= _PRICE_TOLERANCE


def _date_in_range(
    check: Optional[date],
    start: Optional[date],
    end: Optional[date],
) -> bool:
    """Return True if *check* falls within [start, end] (inclusive, None = open)."""
    if check is None:
        return True
    if start and check < start:
        return False
    if end and check > end:
        return False
    return True


def _month_to_date(reporting_month: str) -> Optional[date]:
    try:
        year, month = reporting_month.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return None


def _parse_date(value: object) -> Optional[date]:
    if not value or str(value).strip() in ("", "nan", "None"):
        return None
    import datetime as dt

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None
