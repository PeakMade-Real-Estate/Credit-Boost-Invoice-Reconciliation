"""
Property matching engine.

Matches each invoice line to a ``PropertyMaster`` record using the following
priority:

  1. Vendor Property ID  (exact)
  2. Internal Property ID (exact)
  3. PMS Property ID  (exact)
  4. Approved property alias  (exact, normalised)
  5. Exact normalised property name
  6. Fuzzy name match â€“ **suggestion only**, never auto-approved

Fuzzy matches set ``match_status = FUZZY_SUGGESTION`` and
``requires_user_review = True``.  They are **never** silently accepted.

Usage::

    from services.property_matching import match_invoice_properties

    lines, exceptions = match_invoice_properties(
        invoice_lines=lines,
        property_master=properties,
        property_aliases=aliases,
        vendor="Rent Plus",
        reporting_month="2026-07",
        fuzzy_threshold=80,
    )
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

from models.reconciliation_models import (
    ExceptionSeverity,
    ExceptionType,
    InvoiceLine,
    MatchStatus,
    PropertyException,
    PropertyMaster,
)
from services.utils import normalize_text

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, process as rf_process

    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RAPIDFUZZ_AVAILABLE = False
    logger.warning(
        "rapidfuzz is not installed.  Fuzzy property matching will be disabled."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_invoice_properties(
    invoice_lines: List[InvoiceLine],
    property_master: List[PropertyMaster],
    property_aliases: List,  # List[PropertyAlias]
    vendor: str,
    reporting_month: str,
    *,
    fuzzy_threshold: int = 80,
    manual_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[List[InvoiceLine], List[PropertyException]]:
    """Attempt to match every invoice line to a known property.

    Args:
        invoice_lines:    Lines produced by the invoice parser.
        property_master:  Full list of ``PropertyMaster`` records.
        property_aliases: Approved alias records.
        vendor:           Vendor being reconciled (used to detect wrong-vendor).
        reporting_month:  YYYY-MM string (used for date-range validation).
        fuzzy_threshold:  Minimum rapidfuzz score (0-100) for suggestions.
        manual_overrides: Optional dict mapping invoice property name â†’
                          internal_property_id (from the exceptions page).

    Returns:
        Tuple of (updated invoice_lines, list of PropertyException).
    """
    overrides = {
        normalize_text(k): v
        for k, v in (manual_overrides or {}).items()
    }

    # Build look-up indices
    by_vendor_id: Dict[str, PropertyMaster] = {
        p.vendor_property_id: p
        for p in property_master
        if p.vendor_property_id
    }
    by_internal_id: Dict[str, PropertyMaster] = {
        p.internal_property_id: p for p in property_master
    }
    by_pms_id: Dict[str, PropertyMaster] = {
        p.pms_property_id: p for p in property_master if p.pms_property_id
    }
    # Only use approved aliases â€“ check flag even if caller already filtered
    alias_index: Dict[str, str] = {
        a.alias: a.internal_property_id
        for a in property_aliases
        if a.approved
    }
    by_normalized_name: Dict[str, PropertyMaster] = {
        p.normalized_property_name: p for p in property_master
    }

    exceptions: List[PropertyException] = []
    report_date = _reporting_month_to_date(reporting_month)

    for line in invoice_lines:
        matched_property: Optional[PropertyMaster] = None
        match_method: Optional[MatchStatus] = None

        norm_name = line.normalized_property_name
        vendor_pid = line.vendor_property_id.strip()

        # 0. Manual override (from exceptions page)
        if norm_name in overrides:
            internal_id = overrides[norm_name]
            matched_property = by_internal_id.get(internal_id)
            if matched_property:
                match_method = MatchStatus.MATCHED
                line.manual_override = True
                logger.debug(
                    "Row %d: manual override â†’ %s", line.source_row, internal_id
                )

        # 1. Vendor Property ID
        if matched_property is None and vendor_pid:
            matched_property = by_vendor_id.get(vendor_pid)
            if matched_property:
                match_method = MatchStatus.ID_MATCH

        # 2. Internal Property ID (if vendor_property_id looks like internal)
        if matched_property is None and vendor_pid:
            matched_property = by_internal_id.get(vendor_pid)
            if matched_property:
                match_method = MatchStatus.ID_MATCH

        # 3. PMS Property ID
        if matched_property is None and vendor_pid:
            matched_property = by_pms_id.get(vendor_pid)
            if matched_property:
                match_method = MatchStatus.ID_MATCH

        # 4. Approved alias
        if matched_property is None:
            internal_id = alias_index.get(norm_name)
            if internal_id:
                matched_property = by_internal_id.get(internal_id)
                if matched_property:
                    match_method = MatchStatus.ALIAS_MATCH

        # 5. Exact normalised name
        if matched_property is None:
            matched_property = by_normalized_name.get(norm_name)
            if matched_property:
                match_method = MatchStatus.MATCHED

        # 6. Fuzzy match (suggestion only)
        if matched_property is None and _RAPIDFUZZ_AVAILABLE:
            candidates = list(by_normalized_name.keys())
            if candidates:
                best_match, score, _ = rf_process.extractOne(
                    norm_name,
                    candidates,
                    scorer=fuzz.token_sort_ratio,
                )
                if score >= fuzzy_threshold:
                    suggested = by_normalized_name[best_match]
                    exceptions.append(
                        PropertyException(
                            exception_type=ExceptionType.FUZZY_MATCH_SUGGESTION,
                            severity=ExceptionSeverity.BLOCKING,
                            invoice_property_name=line.extracted_property_name or line.original_description,
                            invoice_vendor_property_id=vendor_pid or None,
                            suggested_property_id=suggested.internal_property_id,
                            suggested_property_name=suggested.property_name,
                            confidence=round(score / 100, 4),
                            requires_user_review=True,
                            message=(
                                f"Row {line.source_row}: "
                                f"{(line.extracted_property_name or line.original_description)!r} closely matches "
                                f"{suggested.property_name!r} "
                                f"(score {score:.0f}/100).  "
                                "Manual confirmation required."
                            ),
                        )
                    )
                    line.match_status = MatchStatus.FUZZY_SUGGESTION
                    line.exception_reason = (
                        f"Fuzzy suggestion: {suggested.property_name} "
                        f"({score:.0f}/100)"
                    )
                    continue  # Skip further processing until user resolves

        # --- Evaluate matched result ---
        if matched_property is None:
            line.match_status = MatchStatus.UNMATCHED
            line.exception_reason = "No matching property found"
            exceptions.append(
                PropertyException(
                    exception_type=ExceptionType.UNMATCHED_PROPERTY,
                    severity=ExceptionSeverity.BLOCKING,
                    invoice_property_name=line.extracted_property_name or line.original_description,
                    invoice_vendor_property_id=vendor_pid or None,
                    requires_user_review=True,
                    message=(
                        f"Row {line.source_row}: "
                        f"Invoice property {(line.extracted_property_name or line.original_description)!r} "
                        "could not be matched to any known property."
                    ),
                )
            )
            continue

        # Stamp match info onto line
        line.internal_property_id = matched_property.internal_property_id
        line.pms_property_id = matched_property.pms_property_id
        line.match_status = match_method or MatchStatus.MATCHED

        # --- Post-match validations ---
        _validate_matched_property(
            line, matched_property, vendor, report_date, exceptions
        )

    # --- Portfolio-level checks ---
    _check_expected_properties_missing(
        invoice_lines, property_master, vendor, exceptions
    )
    _check_unexpected_properties(invoice_lines, property_master, exceptions)

    logger.info(
        "Property matching complete.  %d lines, %d exceptions.",
        len(invoice_lines),
        len(exceptions),
    )
    return invoice_lines, exceptions


# ---------------------------------------------------------------------------
# Post-match validations
# ---------------------------------------------------------------------------


def _validate_matched_property(
    line: InvoiceLine,
    prop: PropertyMaster,
    vendor: str,
    report_date: Optional[date],
    exceptions: List[PropertyException],
) -> None:
    """Check vendor assignment, date ranges, and active status."""
    # Wrong vendor
    if prop.vendor.strip().lower() != vendor.strip().lower():
        exceptions.append(
            PropertyException(
                exception_type=ExceptionType.WRONG_VENDOR,
                severity=ExceptionSeverity.BLOCKING,
                invoice_property_name=line.extracted_property_name or line.original_description,
                suggested_property_id=prop.internal_property_id,
                suggested_property_name=prop.property_name,
                message=(
                    f"Row {line.source_row}: Property {prop.property_name!r} is "
                    f"assigned to vendor {prop.vendor!r}, not {vendor!r}."
                ),
            )
        )

    if report_date is None:
        return

    # Billed before start date
    if prop.program_start_date and report_date < prop.program_start_date:
        exceptions.append(
            PropertyException(
                exception_type=ExceptionType.BILLED_BEFORE_START,
                severity=ExceptionSeverity.BLOCKING,
                invoice_property_name=line.extracted_property_name or line.original_description,
                suggested_property_id=prop.internal_property_id,
                suggested_property_name=prop.property_name,
                message=(
                    f"Row {line.source_row}: {prop.property_name!r} program "
                    f"starts {prop.program_start_date}, but reporting month "
                    f"is {report_date}."
                ),
            )
        )

    # Billed after end date
    if prop.program_end_date and report_date > prop.program_end_date:
        exceptions.append(
            PropertyException(
                exception_type=ExceptionType.BILLED_AFTER_END,
                severity=ExceptionSeverity.BLOCKING,
                invoice_property_name=line.extracted_property_name or line.original_description,
                suggested_property_id=prop.internal_property_id,
                suggested_property_name=prop.property_name,
                message=(
                    f"Row {line.source_row}: {prop.property_name!r} program "
                    f"ended {prop.program_end_date}, but reporting month "
                    f"is {report_date}."
                ),
            )
        )


def _check_expected_properties_missing(
    invoice_lines: List[InvoiceLine],
    property_master: List[PropertyMaster],
    vendor: str,
    exceptions: List[PropertyException],
) -> None:
    """Raise a WARNING for every active vendor property not found on invoice."""
    invoiced_internal_ids = {
        ln.internal_property_id
        for ln in invoice_lines
        if ln.internal_property_id
    }
    for prop in property_master:
        if (
            prop.vendor.strip().lower() == vendor.strip().lower()
            and prop.active
            and prop.internal_property_id not in invoiced_internal_ids
        ):
            exceptions.append(
                PropertyException(
                    exception_type=ExceptionType.MISSING_EXPECTED_PROPERTY,
                    severity=ExceptionSeverity.WARNING,
                    suggested_property_id=prop.internal_property_id,
                    suggested_property_name=prop.property_name,
                    message=(
                        f"Expected active property {prop.property_name!r} "
                        f"({prop.internal_property_id}) was not found on the invoice."
                    ),
                )
            )


def _check_unexpected_properties(
    invoice_lines: List[InvoiceLine],
    property_master: List[PropertyMaster],
    exceptions: List[PropertyException],
) -> None:
    """Raise an INFO for invoice lines that matched an inactive property."""
    active_ids = {p.internal_property_id for p in property_master if p.active}
    seen: set = set()
    for line in invoice_lines:
        if (
            line.internal_property_id
            and line.internal_property_id not in active_ids
            and line.internal_property_id not in seen
        ):
            seen.add(line.internal_property_id)
            exceptions.append(
                PropertyException(
                    exception_type=ExceptionType.UNEXPECTED_PROPERTY,
                    severity=ExceptionSeverity.WARNING,
                    invoice_property_name=line.extracted_property_name or line.original_description,
                    suggested_property_id=line.internal_property_id,
                    message=(
                        f"Property {(line.extracted_property_name or line.original_description)!r} matched an "
                        f"inactive property master record "
                        f"({line.internal_property_id})."
                    ),
                )
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reporting_month_to_date(reporting_month: str) -> Optional[date]:
    """Convert YYYY-MM to the first day of that month."""
    try:
        from datetime import date as _date
        year, month = reporting_month.split("-")
        return _date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return None

