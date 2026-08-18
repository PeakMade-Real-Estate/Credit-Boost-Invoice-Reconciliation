"""
Abstract base class for vendor reconciliation strategies.

Each vendor is represented by a concrete subclass.  The strategy controls
how vendor files are parsed, how properties are matched, how quantities and
amounts are calculated, and which reference data tables are loaded.

To add a new vendor:

1. Subclass ``VendorReconciliationStrategy``.
2. Implement every abstract method.
3. Add a YAML profile to ``config/vendors/<vendor_code>.yaml``.
4. Register the class in ``services/vendor_strategy/__init__.py``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from models.reconciliation_models import (
    InvoiceSummary,
    PropertyException,
    PropertyMaster,
    PropertyResult,
    RateException,
    ValidationResult,
)


class VendorReconciliationStrategy(ABC):
    """Interface for vendor-specific reconciliation logic.

    The shared ``run_reconciliation()`` orchestrator calls these methods in
    order, delegating all vendor-specific behaviour to the active strategy.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def vendor_code(self) -> str:
        """Canonical snake_case vendor code (e.g. ``"rent_plus"``)."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable vendor name used for display and logging."""

    @property
    @abstractmethod
    def accepted_file_types(self) -> frozenset:
        """File extensions accepted without leading dot (e.g. ``frozenset({"csv", "xlsx"})``)."""

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------

    @abstractmethod
    def load_vendor_reference_data(
        self,
        *,
        rate_mapping_path: Optional[str] = None,
        property_aliases_path: Optional[str] = None,
        property_rollups_path: Optional[str] = None,
    ) -> dict:
        """Load vendor-specific reference tables.

        Explicit path arguments override the YAML-configured defaults.

        Returned dict keys by vendor:

        - RentPlus: ``"rate_rules"`` (List[RateRule]),
          ``"property_aliases"`` (List[PropertyAlias])
        - Boom: ``"rollups"`` (Dict[str, BoomPropertyRollup],
          keyed by normalised boom_property_id)
        """

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_vendor_file(
        self,
        file_path: str,
        reporting_month: str,
    ) -> Tuple[list, InvoiceSummary]:
        """Parse the vendor file; return ``(raw_lines, invoice_summary)``.

        ``raw_lines`` is a list of vendor-specific line objects
        (``InvoiceLine`` for RentPlus, ``BoomTransactionLine`` for Boom).
        """

    # ------------------------------------------------------------------
    # Normalisation / filtering
    # ------------------------------------------------------------------

    @abstractmethod
    def normalize_vendor_data(self, raw_lines: list) -> list:
        """Filter and normalise raw lines.

        - RentPlus: returns lines unchanged.
        - Boom: sets ``is_qualifying`` on each line according to the
          configured Category / Transaction Type / Template Name / Subject Type
          filter values.  Non-qualifying lines are retained in the list so
          they appear in audit output, but are excluded from aggregation.
        """

    # ------------------------------------------------------------------
    # Vendor-specific validation (before property matching)
    # ------------------------------------------------------------------

    @abstractmethod
    def validate_vendor_data(
        self,
        lines: list,
        invoice_summary: InvoiceSummary,
    ) -> List[ValidationResult]:
        """Run vendor-specific checks before property matching.

        - RentPlus: returns an empty list.
        - Boom: raises a BLOCKING ``ValidationResult`` if any qualifying
          transaction ID appears more than once.
        """

    # ------------------------------------------------------------------
    # Property matching
    # ------------------------------------------------------------------

    @abstractmethod
    def match_to_properties(
        self,
        lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        fuzzy_threshold: int = 80,
        manual_overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[list, List[PropertyException]]:
        """Match each line to a ``PropertyMaster`` record.

        - RentPlus: uses the 6-priority matching engine
          (vendor ID → internal ID → PMS ID → alias → exact name → fuzzy).
        - Boom: uses the approved rollup mapping
          (boom_property_id → internal_property_id).

        Returns ``(updated_lines, property_exceptions)``.
        """

    # ------------------------------------------------------------------
    # Aggregation and rate / quantity calculation
    # ------------------------------------------------------------------

    @abstractmethod
    def aggregate_and_rate_map(
        self,
        matched_lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        manual_rate_overrides: Optional[Dict[int, str]] = None,
    ) -> Tuple[List[PropertyResult], List[RateException]]:
        """Aggregate matched lines to one ``PropertyResult`` per property.

        QTY and AMOUNT are calculated differently per vendor:

        - **RentPlus**: applies configurable rate multipliers and sign;
          ``net_policy_quantity = sum(original_qty × multiplier × sign)``.
        - **Boom**: counts distinct qualifying transaction IDs per property
          (``net_policy_quantity``); sums raw transaction amounts
          (``invoice_amount_owed``).  No rate exceptions are produced.
        """

    # ------------------------------------------------------------------
    # Per-property financial calculations
    # ------------------------------------------------------------------

    @abstractmethod
    def calculate_property_financials(
        self,
        property_results: List[PropertyResult],
        matched_lines: list,
        vendor_reference_data: dict,
    ) -> List[PropertyResult]:
        """Populate financial fields (revenue shares, shortfall, status) per property.

        - RentPlus: calculates expected peak / owner / resident shares using
          rate rule per-policy amounts.
        - Boom: calculates actual revenue share from cash vs. amount owed;
          does not apply rate rule multipliers.
        """
