"""
Data models for the Resident Charges vs Invoice reconciliation.

This reconciliation type cross-references the Entrata Resident Charges
report (multi-sheet Excel) against a Boom invoice export to identify:

  - Residents who were charged in Entrata but NOT billed by the vendor.
  - Residents the vendor billed for but who were NOT charged in Entrata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional


@dataclass
class ResidentChargeRecord:
    """One resident row from the Entrata Resident Charges Excel report."""

    property_name: str          # Sheet name, e.g. "Beach Club"
    unit: str                   # Bldg-Unit, e.g. "6801-101"
    resident_name: str          # As printed, e.g. "Valdez, Luis"
    normalized_name: str        # For matching, e.g. "luis valdez"
    lease_status: str           # Current | Notice | Past | Future
    amount: Decimal
    reporting_period: str       # e.g. "Aug 2026"


@dataclass
class BoomResidentRecord:
    """One qualifying resident-level line from the Boom invoice export."""

    property_name: str          # Property Name column value
    unit: str                   # Unit column value
    resident_name: str          # Name column value, e.g. "Luis Valdez"
    normalized_name: str        # For matching, e.g. "luis valdez"
    amount: Decimal
    transaction_id: str


@dataclass
class ResidentDiscrepancy:
    """A single discrepancy found during resident charges reconciliation."""

    discrepancy_type: str       # "charged_not_billed" | "billed_not_charged"
    property_name: str
    unit: str
    resident_name: str          # Display name from source
    lease_status: str           # From Resident Charges; blank for billed_not_charged
    charged_amount: Decimal     # From Resident Charges; 0 if not charged
    billed_amount: Decimal      # From Boom invoice; 0 if not billed


@dataclass
class PropertyChargesummary:
    """Per-property summary for a resident charges reconciliation run."""

    property_name: str
    residents_charged: int      # Rows with amount > 0 in Resident Charges
    residents_billed: int       # Qualifying Boom invoice lines
    matched_count: int
    charged_not_billed_count: int
    billed_not_charged_count: int
    total_charged_amount: Decimal
    total_billed_amount: Decimal


@dataclass
class ResidentChargesReconciliationResult:
    """Complete result of a resident charges vs Boom invoice reconciliation."""

    run_id: str
    reporting_month: str
    vendor: str
    charges_file_name: str
    invoice_file_name: str

    property_summaries: List[PropertyChargesummary] = field(default_factory=list)
    discrepancies: List[ResidentDiscrepancy] = field(default_factory=list)
    unmatched_charge_properties: List[str] = field(default_factory=list)
    unmatched_invoice_properties: List[str] = field(default_factory=list)

    total_residents_charged: int = 0
    total_residents_billed: int = 0
    total_matched: int = 0
    total_charged_not_billed: int = 0
    total_billed_not_charged: int = 0
