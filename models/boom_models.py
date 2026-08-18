"""
Data models specific to the Credit Boost powered by Boom reconciliation profile.

Kept separate from ``reconciliation_models.py`` so that vendor-specific data
structures do not pollute the shared model namespace.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from models.reconciliation_models import MatchStatus


@dataclass
class BoomPropertyRollup:
    """Maps a Boom property identifier to the internal Credit Boost property ID.

    Boom does not share the same property-identifier space as Entrata / the
    PMS, so a dedicated roll-up mapping table is required.  Only rows where
    ``approved=True`` are used during property matching.
    """

    boom_property_id: str       # Value as it appears in the Boom export file
    internal_property_id: str   # Our internal ID (e.g. ``"CB-0001"``)
    pms_property_id: str        # Entrata PMS ID (e.g. ``"PMS-101"``)
    boom_property_name: str     # Human-readable Boom property name
    approved: bool              # Only approved mappings are used in matching


@dataclass
class BoomTransactionLine:
    """A single normalised line from a Boom export file (CSV or XLSX).

    Fields are populated in stages:

    1. ``boom_invoice_parser.parse_boom_file`` fills the core fields.
    2. ``BoomReconciliationStrategy.normalize_vendor_data`` sets ``is_qualifying``
       based on the configured filter values.
    3. ``BoomReconciliationStrategy.match_to_properties`` fills the
       property-ID fields via the approved rollup mapping.
    """

    invoice_id: str
    reporting_month: str            # YYYY-MM
    vendor: str
    source_row: int
    transaction_id: str             # Unique Boom transaction identifier
    boom_property_id: str           # Raw Boom property value (not internal ID)
    original_description: str       # Template name or description
    category: str                   # e.g. ``"Credit Boost"``
    transaction_type: str           # e.g. ``"Positive"``, ``"Negative"``
    template_name: str              # e.g. ``"Credit Boost Template"``
    subject_type: str               # e.g. ``"Resident"``
    transaction_amount: Decimal

    # -- Populated by normalize_vendor_data --
    is_qualifying: bool = False

    # -- Populated by match_to_properties --
    internal_property_id: Optional[str] = None
    pms_property_id: Optional[str] = None
    match_status: MatchStatus = MatchStatus.UNMATCHED
    exception_reason: Optional[str] = None
