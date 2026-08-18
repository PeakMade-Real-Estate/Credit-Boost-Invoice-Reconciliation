"""
Core data models for the invoice reconciliation system.

These classes are intentionally independent of Flask, HTTP, or any web
framework so they can be used by:
  - The Flask web application
  - Power Automate (via an API)
  - An Azure Function
  - A scheduled Python process
  - A background job

Use ``from decimal import Decimal`` for all financial arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MatchStatus(str, Enum):
    """The result of attempting to match an invoice line to a property."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    FUZZY_SUGGESTION = "fuzzy_suggestion"
    ALIAS_MATCH = "alias_match"
    ID_MATCH = "id_match"
    EXCLUDED = "excluded"


class ExceptionSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class ExceptionType(str, Enum):
    # -- Property --
    UNEXPECTED_PROPERTY = "unexpected_property"
    MISSING_EXPECTED_PROPERTY = "missing_expected_property"
    UNMATCHED_PROPERTY = "unmatched_property"
    DUPLICATE_PROPERTY = "duplicate_property"
    WRONG_VENDOR = "wrong_vendor"
    BILLED_BEFORE_START = "billed_before_start"
    BILLED_AFTER_END = "billed_after_end"
    FUZZY_MATCH_SUGGESTION = "fuzzy_match_suggestion"
    # -- Rate --
    UNKNOWN_RATE = "unknown_rate"
    LEGACY_RATE_ACTIVE = "legacy_rate_active"
    # -- Cash --
    NO_CASH_RECORD = "no_cash_record"
    NO_INVOICE_RECORD = "no_invoice_record"
    ZERO_CASH = "zero_cash"
    NEGATIVE_CASH = "negative_cash"
    CASH_BELOW_OWED = "cash_below_owed"
    # -- Boom-specific --
    DUPLICATE_TRANSACTION = "duplicate_transaction"
    # -- RentPlus PDF-specific --
    RENTPLUS_PROPERTY_PARSE_FAILURE = "rentplus_property_parse_failure"
    # -- File / run --
    INVOICE_TOTAL_MISMATCH = "invoice_total_mismatch"
    DUPLICATE_FILE = "duplicate_file"
    MISSING_REPORTING_MONTH = "missing_reporting_month"
    CASH_PERIOD_MISMATCH = "cash_period_mismatch"
    DUPLICATE_FINAL_PROPERTY = "duplicate_final_property"
    BALANCE_OUT_OF_TOLERANCE = "balance_out_of_tolerance"


class ReconciliationStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    REQUIRES_REVIEW = "requires_review"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@dataclass
class PropertyMaster:
    """One record from the property master list."""

    internal_property_id: str
    pms_property_id: str
    vendor_property_id: str
    property_name: str
    normalized_property_name: str
    vendor: str
    program_status: str
    program_start_date: Optional[date]
    program_end_date: Optional[date]
    charge_code: str
    active: bool


@dataclass
class PropertyAlias:
    """A known alias mapping an invoice property name to an internal ID."""

    alias: str
    internal_property_id: str
    approved: bool
    created_date: Optional[date]


@dataclass
class RateRule:
    """One configurable rate-mapping rule from rate_mapping.csv."""

    rate_rule_id: str
    vendor: str
    property_id: Optional[str]          # blank = general rule
    invoice_unit_price: Decimal
    rate_name: str
    policy_multiplier: Decimal
    sign: Decimal                        # 1 = positive, -1 = reversal
    vendor_cost_per_policy: Decimal
    peak_share_per_policy: Decimal
    owner_share_per_policy: Decimal
    resident_charge_per_policy: Decimal
    effective_start_date: Optional[date]
    effective_end_date: Optional[date]
    active: bool


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


@dataclass
class InvoiceLine:
    """A single normalised line from the vendor invoice.

    Fields are populated in stages:
      1. invoice_parser fills the core fields.
      2. property_matching fills the property-ID fields and match_status.
      3. rate_mapping fills the rate fields and normalized_policy_quantity.
    """

    invoice_id: str
    reporting_month: str                # YYYY-MM
    vendor: str
    source_row: int
    original_description: str
    normalized_property_name: str
    vendor_property_id: str
    original_quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal

    # -- Populated by rate_mapping --
    rate_rule_id: Optional[str] = None
    rate_type: Optional[str] = None
    policy_multiplier: Optional[Decimal] = None
    sign: Optional[Decimal] = None
    normalized_policy_quantity: Optional[Decimal] = None

    # -- Description parsing (populated by PDF parser) --
    normalized_description: Optional[str] = None
    extracted_property_name: Optional[str] = None
    property_parse_status: str = ""      # "parsed" | "failed" | ""
    property_parse_exception: Optional[str] = None

    # -- Populated by property_matching --
    match_status: MatchStatus = MatchStatus.UNMATCHED
    exception_reason: Optional[str] = None
    internal_property_id: Optional[str] = None
    pms_property_id: Optional[str] = None

    # -- Manual override tracking --
    manual_override: bool = False
    override_user: Optional[str] = None
    override_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Cash report
# ---------------------------------------------------------------------------


@dataclass
class CashRecord:
    """One normalised row from the Entrata cash-received report."""

    reporting_month: str
    pms_property_id: str
    property_name: str
    normalized_property_name: str
    charge_code: str
    cash_received: Decimal
    adjustment_amount: Decimal
    source_file_name: str
    internal_property_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


@dataclass
class PropertyException:
    """An exception raised during property matching."""

    exception_type: ExceptionType
    severity: ExceptionSeverity
    invoice_property_name: Optional[str] = None
    invoice_vendor_property_id: Optional[str] = None
    suggested_property_id: Optional[str] = None
    suggested_property_name: Optional[str] = None
    confidence: Optional[float] = None
    requires_user_review: bool = False
    message: str = ""
    resolved: bool = False
    resolution_note: Optional[str] = None


@dataclass
class RateException:
    """An exception raised during rate matching."""

    exception_type: ExceptionType
    severity: ExceptionSeverity
    invoice_property_name: Optional[str] = None
    invoice_unit_price: Optional[Decimal] = None
    source_row: Optional[int] = None
    message: str = ""
    suggested_rate_rule_id: Optional[str] = None
    resolved: bool = False
    resolution_note: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    severity: ExceptionSeverity
    expected_value: Optional[Any] = None
    actual_value: Optional[Any] = None
    difference: Optional[Any] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Aggregated results
# ---------------------------------------------------------------------------


@dataclass
class PropertyResult:
    """Aggregated reconciliation result for one property."""

    internal_property_id: str
    pms_property_id: str
    vendor_property_id: str
    property_name: str
    reporting_month: str
    charge_code: str = ""

    # Policy quantities
    net_policy_quantity: Decimal = Decimal("0")
    standard_policy_quantity: Decimal = Decimal("0")
    legacy_policy_quantity: Decimal = Decimal("0")
    rate_exception_count: int = 0

    # Invoice amounts
    invoice_amount_owed: Decimal = Decimal("0")
    positive_invoice_amount: Decimal = Decimal("0")
    refund_reversal_amount: Decimal = Decimal("0")

    # Cash
    cash_received: Decimal = Decimal("0")

    # Calculations
    amount_to_pull: Decimal = Decimal("0")
    actual_property_revenue_share: Decimal = Decimal("0")
    expected_peak_revenue_share: Decimal = Decimal("0")
    expected_owner_share: Decimal = Decimal("0")
    property_share_shortfall: Decimal = Decimal("0")
    expected_resident_charges: Decimal = Decimal("0")
    collection_rate: Optional[Decimal] = None

    # Vendor identity and programme metadata
    vendor: str = ""
    transition_date: Optional[date] = None

    # Status
    validation_status: str = ""
    notes: str = ""


@dataclass
class PortfolioTotals:
    """Portfolio-level aggregated totals."""

    total_properties: int = 0
    total_net_policy_quantity: Decimal = Decimal("0")
    total_invoice_amount_owed: Decimal = Decimal("0")
    total_cash_received: Decimal = Decimal("0")
    total_actual_property_revenue_share: Decimal = Decimal("0")
    total_expected_peak_revenue_share: Decimal = Decimal("0")
    balance_difference: Decimal = Decimal("0")
    overall_status: ReconciliationStatus = ReconciliationStatus.REQUIRES_REVIEW


@dataclass
class InvoiceSummary:
    """High-level summary of the parsed vendor invoice."""

    invoice_id: str
    vendor: str
    reporting_month: str
    source_invoice_total: Decimal
    calculated_invoice_total: Decimal
    invoice_difference: Decimal
    invoice_validation_status: str
    line_count: int
    property_count: int
    source_file_name: str
    source_file_hash: str


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationRun:
    """Metadata persisted for every reconciliation run."""

    run_id: str
    reporting_month: str
    vendor: str
    invoice_file_name: str
    cash_report_file_name: str
    invoice_file_hash: str
    cash_report_file_hash: str
    uploaded_by: str
    uploaded_date: datetime
    status: ReconciliationStatus
    invoice_stored_path: str
    cash_report_stored_path: str
    accounting_output_path: str = ""
    audit_output_path: str = ""
    reconciliation_csv_path: str = ""
    exception_count: int = 0
    blocking_exception_count: int = 0
    notes: str = ""

    @staticmethod
    def generate_run_id(
        reporting_month: str,
        vendor: str,
        sequence: int = 1,
    ) -> str:
        """Return a human-readable run ID such as ``REC-202607-RENTPLUS-0001``."""
        vendor_slug = (
            vendor.upper().replace(" ", "").replace("-", "")[:12]
        )
        month_slug = reporting_month.replace("-", "")
        return f"REC-{month_slug}-{vendor_slug}-{sequence:04d}"


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


@dataclass
class ManualOverride:
    """Records a human decision made on the exceptions page."""

    override_id: str
    run_id: str
    override_type: str          # "property_match" | "rate_rule" | "exclude"
    invoice_property_name: str
    resolved_property_id: Optional[str]
    resolved_rate_rule_id: Optional[str]
    excluded: bool
    exclusion_reason: Optional[str]
    override_user: str
    override_date: datetime
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Top-level reconciliation result
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationResult:
    """
    Complete result of one reconciliation run.

    This is the primary return value of ``run_reconciliation()`` and is
    entirely independent of Flask, HTTP, or any web framework.

    Example usage outside Flask::

        from services.reconciliation_service import run_reconciliation

        result = run_reconciliation(
            invoice_file_path="invoice.xlsx",
            cash_report_file_path="cash_report.csv",
            reporting_month="2026-07",
            vendor="Rent Plus",
        )
    """

    status: ReconciliationStatus
    reporting_month: str
    vendor: str
    run_id: str

    invoice_summary: Optional[InvoiceSummary] = None
    property_results: List[PropertyResult] = field(default_factory=list)
    property_exceptions: List[PropertyException] = field(default_factory=list)
    rate_exceptions: List[RateException] = field(default_factory=list)
    validation_results: List[ValidationResult] = field(default_factory=list)
    portfolio_totals: Optional[PortfolioTotals] = None
    invoice_lines: List[InvoiceLine] = field(default_factory=list)
    # Vendor-specific transaction lines (used when vendor_lines differ from InvoiceLine,
    # e.g. BoomTransactionLine objects for the Credit Boost powered by Boom profile)
    vendor_transaction_lines: list = field(default_factory=list)
    cash_records: List[CashRecord] = field(default_factory=list)
    accounting_output_path: str = ""
    audit_output_path: str = ""
    reconciliation_csv_path: str = ""
    errors: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def has_blocking_exceptions(self) -> bool:
        """True if any exception or validation check is BLOCKING."""
        return (
            any(
                e.severity == ExceptionSeverity.BLOCKING
                for e in self.property_exceptions
            )
            or any(
                e.severity == ExceptionSeverity.BLOCKING
                for e in self.rate_exceptions
            )
            or any(
                not v.passed and v.severity == ExceptionSeverity.BLOCKING
                for v in self.validation_results
            )
        )

    @property
    def exception_count(self) -> int:
        return len(self.property_exceptions) + len(self.rate_exceptions)

    @property
    def blocking_exception_count(self) -> int:
        return sum(
            1
            for e in self.property_exceptions
            if e.severity == ExceptionSeverity.BLOCKING
        ) + sum(
            1
            for e in self.rate_exceptions
            if e.severity == ExceptionSeverity.BLOCKING
        )


# Re-export Config alias so ``models`` has no dependency on the config module.
class Config:  # type: ignore[no-redef]
    """Thin alias – import the real Config from config.py."""
    pass
