"""
RentPlus reconciliation strategy.

Implements ``VendorReconciliationStrategy`` for the Rent Plus vendor using
the existing invoice parser, 6-priority property matching engine, and
configurable rate-multiplier quantity calculation.

Reference data comes from ``config/vendors/rentplus.yaml`` (paths can be
overridden at call time for testing or multi-tenant deployments).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models.reconciliation_models import (
    ExceptionSeverity,
    ExceptionType,
    InvoiceLine,
    InvoiceSummary,
    PropertyException,
    PropertyMaster,
    PropertyResult,
    RateException,
    ValidationResult,
)
from services.invoice_parser import parse_invoice
from services.property_master_loader import load_property_aliases
from services.property_matching import match_invoice_properties
from services.rate_mapping import apply_rate_mapping, load_rate_rules
from services.utils import safe_divide
from services.vendor_strategy.base import VendorReconciliationStrategy

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "vendors" / "rentplus.yaml"


class RentPlusReconciliationStrategy(VendorReconciliationStrategy):
    """Reconciliation strategy for Rent Plus invoices.

    Supports CSV, XLSX, and PDF invoice formats.  Quantities are computed
    using configurable rate multipliers (``policy_multiplier × sign``), and
    the rate table supports property-specific legacy rates.
    """

    def __init__(self) -> None:
        import yaml

        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            self._config: dict = yaml.safe_load(fh)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def vendor_code(self) -> str:
        return "rent_plus"

    @property
    def display_name(self) -> str:
        return "Rent Plus"

    @property
    def accepted_file_types(self) -> frozenset:
        return frozenset(self._config.get("accepted_file_types", ["csv", "xlsx", "pdf"]))

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------

    def load_vendor_reference_data(
        self,
        *,
        rate_mapping_path: Optional[str] = None,
        property_aliases_path: Optional[str] = None,
        **_kwargs,
    ) -> dict:
        """Load rate rules and property aliases.

        Explicit paths override the YAML-configured defaults.
        """
        rate_path = rate_mapping_path or str(
            _PROJECT_ROOT / self._config["rate_mapping_path"]
        )
        aliases_path = property_aliases_path or str(
            _PROJECT_ROOT / self._config["property_aliases_path"]
        )
        return {
            "rate_rules": load_rate_rules(rate_path),
            "property_aliases": load_property_aliases(aliases_path),
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_vendor_file(
        self,
        file_path: str,
        reporting_month: str,
    ) -> Tuple[List[InvoiceLine], InvoiceSummary]:
        return parse_invoice(
            file_path,
            vendor=self.display_name,
            reporting_month=reporting_month,
        )

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def normalize_vendor_data(self, raw_lines: list) -> list:
        """RentPlus has no qualifying filter – return lines unchanged."""
        return raw_lines

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_vendor_data(
        self,
        lines: list,
        invoice_summary: InvoiceSummary,
    ) -> List[ValidationResult]:
        """No vendor-specific pre-matching checks for RentPlus."""
        return []

    # ------------------------------------------------------------------
    # Property matching
    # ------------------------------------------------------------------

    def match_to_properties(
        self,
        lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        fuzzy_threshold: int = 80,
        manual_overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[InvoiceLine], List[PropertyException]]:
        return match_invoice_properties(
            invoice_lines=lines,
            property_master=property_master,
            property_aliases=vendor_reference_data["property_aliases"],
            vendor=self.display_name,
            reporting_month=reporting_month,
            fuzzy_threshold=fuzzy_threshold,
            manual_overrides=manual_overrides,
        )

    # ------------------------------------------------------------------
    # Aggregation and rate mapping
    # ------------------------------------------------------------------

    def aggregate_and_rate_map(
        self,
        matched_lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        manual_rate_overrides: Optional[Dict[int, str]] = None,
    ) -> Tuple[List[PropertyResult], List[RateException]]:
        """Apply rate multipliers, then aggregate to one row per property."""
        rate_rules = vendor_reference_data["rate_rules"]

        # Apply rate mapping: fills normalized_policy_quantity on each line
        matched_lines, rate_exceptions = apply_rate_mapping(
            invoice_lines=matched_lines,
            rate_rules=rate_rules,
            reporting_month=reporting_month,
            manual_rate_overrides=manual_rate_overrides,
        )

        property_results = _aggregate_rentplus_results(
            matched_lines, property_master, reporting_month
        )
        return property_results, rate_exceptions

    # ------------------------------------------------------------------
    # Financial calculations
    # ------------------------------------------------------------------

    def calculate_property_financials(
        self,
        property_results: List[PropertyResult],
        matched_lines: list,
        vendor_reference_data: dict,
    ) -> List[PropertyResult]:
        return _calculate_rentplus_financials(
            property_results, matched_lines, vendor_reference_data["rate_rules"]
        )


# ---------------------------------------------------------------------------
# Private helpers (RentPlus-specific)
# ---------------------------------------------------------------------------


def _aggregate_rentplus_results(
    invoice_lines: List[InvoiceLine],
    property_master: List[PropertyMaster],
    reporting_month: str,
) -> List[PropertyResult]:
    """Produce one ``PropertyResult`` per property from RentPlus invoice lines.

    Quantity is summed using ``normalized_policy_quantity``
    (= ``original_qty × policy_multiplier × sign``), which means reversal
    lines (sign = -1) reduce the net quantity.
    """
    pm_by_internal: Dict[str, PropertyMaster] = {
        p.internal_property_id: p for p in property_master
    }

    # Group lines by internal_property_id
    groups: Dict[str, List[InvoiceLine]] = {}
    for line in invoice_lines:
        pid = line.internal_property_id or f"__unmatched__{line.original_description}"
        groups.setdefault(pid, []).append(line)

    results: List[PropertyResult] = []
    for internal_id, lines in groups.items():
        first = lines[0]
        pm = pm_by_internal.get(internal_id)

        pr = PropertyResult(
            internal_property_id=internal_id,
            pms_property_id=first.pms_property_id or (pm.pms_property_id if pm else ""),
            vendor_property_id=first.vendor_property_id or (pm.vendor_property_id if pm else ""),
            property_name=pm.property_name if pm else first.original_description,
            reporting_month=reporting_month,
            charge_code=pm.charge_code if pm else "",
            vendor="Rent Plus",
            transition_date=pm.program_start_date if pm else None,
        )

        for line in lines:
            amount = line.line_amount
            qty = line.normalized_policy_quantity or Decimal("0")

            if amount >= Decimal("0"):
                pr.positive_invoice_amount += amount
            else:
                pr.refund_reversal_amount += amount

            pr.invoice_amount_owed += amount
            pr.net_policy_quantity += qty

            # Classify standard vs legacy
            if line.rate_type and "legacy" in line.rate_type.lower():
                pr.legacy_policy_quantity += qty
            else:
                pr.standard_policy_quantity += qty

            if line.rate_rule_id is None and line.match_status.value != "fuzzy_suggestion":
                pr.rate_exception_count += 1

        results.append(pr)

    return results


def _calculate_rentplus_financials(
    property_results: List[PropertyResult],
    invoice_lines: List[InvoiceLine],
    rate_rules: list,
) -> List[PropertyResult]:
    """Calculate expected revenue-share metrics using RentPlus rate multipliers."""
    rules_by_id: Dict[str, object] = {r.rate_rule_id: r for r in rate_rules}

    lines_by_property: Dict[str, List[InvoiceLine]] = {}
    for line in invoice_lines:
        pid = line.internal_property_id or ""
        lines_by_property.setdefault(pid, []).append(line)

    for pr in property_results:
        pr.amount_to_pull = pr.invoice_amount_owed
        pr.actual_property_revenue_share = pr.cash_received - pr.invoice_amount_owed

        lines = lines_by_property.get(pr.internal_property_id, [])
        expected_peak = Decimal("0")
        expected_owner = Decimal("0")
        expected_resident = Decimal("0")

        for line in lines:
            rule = rules_by_id.get(line.rate_rule_id) if line.rate_rule_id else None
            if rule and line.normalized_policy_quantity is not None:
                qty = line.normalized_policy_quantity
                expected_peak += qty * rule.peak_share_per_policy
                expected_owner += qty * rule.owner_share_per_policy
                expected_resident += qty * rule.resident_charge_per_policy

        pr.expected_peak_revenue_share = expected_peak
        pr.expected_owner_share = expected_owner
        pr.expected_resident_charges = expected_resident
        pr.property_share_shortfall = expected_owner - pr.actual_property_revenue_share

        collection_rate = safe_divide(pr.cash_received, pr.expected_resident_charges)
        pr.collection_rate = collection_rate

        if pr.rate_exception_count > 0:
            pr.validation_status = "exception"
        elif pr.cash_received < pr.invoice_amount_owed:
            pr.validation_status = "warning"
        else:
            pr.validation_status = "ok"

    return property_results
