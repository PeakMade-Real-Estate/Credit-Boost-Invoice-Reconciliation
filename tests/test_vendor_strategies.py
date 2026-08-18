"""
Unit tests for the vendor reconciliation strategy pattern.

Proves that:
  1. RentPlus uses multiplier-based quantity calculations.
  2. Boom uses unique qualifying transaction counts.
  3. Boom does not use RentPlus rate mappings.
  4. RentPlus reversal rows reduce quantity.
  5. Boom duplicate transaction IDs create blocking exceptions.
  6. Both profiles produce the same standard accounting output schema.
  7. Vendor-specific parsing logic remains isolated from Flask routes.
"""
from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List

import pytest

from models.boom_models import BoomPropertyRollup, BoomTransactionLine
from models.reconciliation_models import (
    ExceptionSeverity,
    InvoiceLine,
    InvoiceSummary,
    MatchStatus,
    PropertyMaster,
)

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPORTING_MONTH = "2026-07"


def _make_invoice_summary(vendor: str = "Rent Plus") -> InvoiceSummary:
    return InvoiceSummary(
        invoice_id="TEST-INV",
        vendor=vendor,
        reporting_month=_REPORTING_MONTH,
        source_invoice_total=Decimal("0"),
        calculated_invoice_total=Decimal("0"),
        invoice_difference=Decimal("0"),
        invoice_validation_status="ok",
        line_count=0,
        property_count=0,
        source_file_name="test.csv",
        source_file_hash="abc123",
    )


def _make_rentplus_line(
    *,
    source_row: int = 2,
    qty: Decimal = Decimal("1"),
    unit_price: Decimal = Decimal("7.85"),
    internal_id: str = "CB-0001",
    pms_id: str = "PMS-101",
) -> InvoiceLine:
    return InvoiceLine(
        invoice_id="TEST-INV",
        reporting_month=_REPORTING_MONTH,
        vendor="Rent Plus",
        source_row=source_row,
        original_description="Test Property",
        normalized_property_name="test property",
        vendor_property_id="",
        original_quantity=qty,
        unit_price=unit_price,
        line_amount=qty * unit_price,
        internal_property_id=internal_id,
        pms_property_id=pms_id,
        match_status=MatchStatus.MATCHED,
    )


def _make_boom_line(
    *,
    source_row: int = 2,
    transaction_id: str = "TXN-001",
    boom_property_id: str = "Summit Ridge",
    category: str = "Credit Boost",
    transaction_type: str = "Positive",
    template_name: str = "Credit Boost Template",
    subject_type: str = "Resident",
    amount: Decimal = Decimal("9.99"),
    internal_id: str = "CB-0010",
    pms_id: str = "PMS-110",
    is_qualifying: bool = True,
) -> BoomTransactionLine:
    line = BoomTransactionLine(
        invoice_id="TEST-INV",
        reporting_month=_REPORTING_MONTH,
        vendor="Credit Boost powered by Boom",
        source_row=source_row,
        transaction_id=transaction_id,
        boom_property_id=boom_property_id,
        original_description=template_name,
        category=category,
        transaction_type=transaction_type,
        template_name=template_name,
        subject_type=subject_type,
        transaction_amount=amount,
        is_qualifying=is_qualifying,
        internal_property_id=internal_id,
        pms_property_id=pms_id,
        match_status=MatchStatus.ALIAS_MATCH,
    )
    return line


def _make_boom_property_master() -> List[PropertyMaster]:
    return [
        PropertyMaster(
            internal_property_id="CB-0010",
            pms_property_id="PMS-110",
            vendor_property_id="",
            property_name="Summit Ridge",
            normalized_property_name="summit ridge",
            vendor="Credit Boost powered by Boom",
            program_status="active",
            program_start_date=date(2024, 6, 1),
            program_end_date=None,
            charge_code="CREDITBOOST",
            active=True,
        ),
        PropertyMaster(
            internal_property_id="CB-0011",
            pms_property_id="PMS-111",
            vendor_property_id="",
            property_name="Maple Grove",
            normalized_property_name="maple grove",
            vendor="Credit Boost powered by Boom",
            program_status="active",
            program_start_date=date(2024, 8, 1),
            program_end_date=None,
            charge_code="CREDITBOOST",
            active=True,
        ),
    ]


def _make_boom_vendor_ref() -> dict:
    return {
        "rollups": {
            "summit ridge": BoomPropertyRollup(
                boom_property_id="Summit Ridge",
                internal_property_id="CB-0010",
                pms_property_id="PMS-110",
                boom_property_name="Summit Ridge",
                approved=True,
            ),
            "maple grove": BoomPropertyRollup(
                boom_property_id="Maple Grove",
                internal_property_id="CB-0011",
                pms_property_id="PMS-111",
                boom_property_name="Maple Grove",
                approved=True,
            ),
        }
    }


# ---------------------------------------------------------------------------
# Test 1 – RentPlus uses multiplier-based quantity calculations
# ---------------------------------------------------------------------------


class TestRentPlusMultiplierQuantity:
    """RentPlus QTY = original_quantity × policy_multiplier × sign."""

    def test_single_rate_multiplier_one(self, rate_rules, property_master):
        """Unit price $7.85 → multiplier=1 → qty unchanged."""
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [_make_rentplus_line(qty=Decimal("10"), unit_price=Decimal("7.85"))]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) == 1
        # multiplier=1, sign=1 → net_policy_quantity = 10 × 1 × 1 = 10
        assert results[0].net_policy_quantity == Decimal("10")

    def test_double_rate_multiplier_two(self, rate_rules, property_master):
        """Unit price $15.70 → multiplier=2 → qty doubled."""
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [_make_rentplus_line(qty=Decimal("5"), unit_price=Decimal("15.70"))]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) == 1
        # multiplier=2, sign=1 → 5 × 2 × 1 = 10
        assert results[0].net_policy_quantity == Decimal("10")


# ---------------------------------------------------------------------------
# Test 4 – RentPlus reversal rows reduce quantity
# ---------------------------------------------------------------------------


class TestRentPlusReversalReducesQuantity:
    """Reversal rate rules have sign=-1; they must reduce net_policy_quantity."""

    def test_reversal_sign_reduces_net_quantity(self, rate_rules, property_master):
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [
            _make_rentplus_line(
                source_row=2, qty=Decimal("10"), unit_price=Decimal("7.85")
            ),
            _make_rentplus_line(
                source_row=3, qty=Decimal("2"), unit_price=Decimal("-7.85")
            ),
        ]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) == 1
        # Forward: 10 × 1 × 1 = 10; Reversal: 2 × 1 × -1 = -2; Net = 8
        assert results[0].net_policy_quantity == Decimal("8")

    def test_reversal_reduces_invoice_amount(self, rate_rules, property_master):
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [
            _make_rentplus_line(qty=Decimal("10"), unit_price=Decimal("7.85")),
            _make_rentplus_line(
                source_row=3, qty=Decimal("1"), unit_price=Decimal("-7.85")
            ),
        ]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        # invoice_amount_owed = 10×7.85 + 1×(-7.85) = 78.50 - 7.85 = 70.65
        assert results[0].invoice_amount_owed == Decimal("70.65")


# ---------------------------------------------------------------------------
# Test 2 – Boom uses unique qualifying transaction counts
# ---------------------------------------------------------------------------


class TestBoomUniqueTransactionCount:
    """Boom QTY = count of distinct qualifying transaction IDs per property."""

    def test_three_distinct_tx_ids_give_qty_three(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-001"),
            _make_boom_line(transaction_id="TXN-002", source_row=3),
            _make_boom_line(transaction_id="TXN-003", source_row=4),
        ]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) == 1
        assert results[0].net_policy_quantity == Decimal("3")

    def test_amount_is_qty_times_flat_rate(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-001", amount=Decimal("9.99")),
            _make_boom_line(transaction_id="TXN-002", amount=Decimal("9.99"), source_row=3),
        ]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        # 2 residents × $6.50 flat rate
        assert results[0].invoice_amount_owed == Decimal("13.00")

    def test_two_properties_counted_separately(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-001", boom_property_id="Summit Ridge",
                            internal_id="CB-0010", pms_id="PMS-110"),
            _make_boom_line(transaction_id="TXN-002", boom_property_id="Maple Grove",
                            internal_id="CB-0011", pms_id="PMS-111", source_row=3),
            _make_boom_line(transaction_id="TXN-003", boom_property_id="Maple Grove",
                            internal_id="CB-0011", pms_id="PMS-111", source_row=4),
        ]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        by_id = {r.internal_property_id: r for r in results}
        assert by_id["CB-0010"].net_policy_quantity == Decimal("1")
        assert by_id["CB-0011"].net_policy_quantity == Decimal("2")


# ---------------------------------------------------------------------------
# Test 3 – Boom does not use RentPlus rate mappings
# ---------------------------------------------------------------------------


class TestBoomNoRateMappings:
    """Boom aggregate_and_rate_map must never produce rate exceptions or
    look up rate rules."""

    def test_rate_exceptions_always_empty(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-001"),
            _make_boom_line(transaction_id="TXN-002", source_row=3),
        ]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        _, rate_exceptions = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        assert rate_exceptions == []

    def test_vendor_ref_contains_no_rate_rules(self, tmp_path):
        """load_vendor_reference_data must not return a 'rate_rules' key."""
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        # Create a minimal rollup file
        rollup_file = tmp_path / "rollups.csv"
        rollup_file.write_text(
            "boom_property_id,internal_property_id,pms_property_id,boom_property_name,approved\n"
            "Summit Ridge,CB-0010,PMS-110,Summit Ridge,true\n"
        )

        strategy = BoomReconciliationStrategy()
        vendor_ref = strategy.load_vendor_reference_data(
            property_rollups_path=str(rollup_file)
        )

        assert "rate_rules" not in vendor_ref
        assert "property_aliases" not in vendor_ref
        assert "rollups" in vendor_ref


# ---------------------------------------------------------------------------
# Test 5 – Boom duplicate transaction IDs create blocking exceptions
# ---------------------------------------------------------------------------


class TestBoomDuplicateTransactionBlocking:
    """Duplicate qualifying transaction IDs must produce a BLOCKING exception."""

    def test_duplicate_tx_id_produces_blocking_validation(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-DUPE"),
            _make_boom_line(transaction_id="TXN-DUPE", source_row=3),
        ]

        summary = _make_invoice_summary("Credit Boost powered by Boom")
        results = strategy.validate_vendor_data(lines, summary)

        blocking = [r for r in results if not r.passed and r.severity == ExceptionSeverity.BLOCKING]
        assert len(blocking) == 1
        assert "TXN-DUPE" in blocking[0].message

    def test_no_duplicates_passes_validation(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-001"),
            _make_boom_line(transaction_id="TXN-002", source_row=3),
        ]

        summary = _make_invoice_summary("Credit Boost powered by Boom")
        results = strategy.validate_vendor_data(lines, summary)

        assert all(r.passed for r in results)

    def test_non_qualifying_duplicates_do_not_block(self):
        """Duplicate IDs on non-qualifying rows should NOT raise BLOCKING."""
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [
            _make_boom_line(transaction_id="TXN-DUPE"),
            _make_boom_line(transaction_id="TXN-DUPE", source_row=3),
        ]
        for line in lines:
            line.is_qualifying = False  # explicitly non-qualifying

        summary = _make_invoice_summary("Credit Boost powered by Boom")
        results = strategy.validate_vendor_data(lines, summary)

        blocking = [r for r in results if not r.passed and r.severity == ExceptionSeverity.BLOCKING]
        assert len(blocking) == 0


# ---------------------------------------------------------------------------
# Test 6 – Both profiles produce the same standard accounting output schema
# ---------------------------------------------------------------------------

# Required spec fields mapped to PropertyResult attribute names
_REQUIRED_FIELDS = {
    "reporting_month",
    "internal_property_id",
    "pms_property_id",
    "property_name",     # "Property"
    "net_policy_quantity",  # "QTY"
    "invoice_amount_owed",  # "AMOUNT"
    "cash_received",
    "actual_property_revenue_share",  # "PA Rev Share"
    "transition_date",
    "notes",
    "vendor",
    "validation_status",
}


class TestCommonOutputSchema:
    """PropertyResult must expose all spec-required accounting fields."""

    def test_rentplus_property_result_has_required_fields(self, rate_rules, property_master):
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [_make_rentplus_line()]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) >= 1
        pr = results[0]
        for field in _REQUIRED_FIELDS:
            assert hasattr(pr, field), f"PropertyResult missing field: {field!r}"

    def test_boom_property_result_has_required_fields(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [_make_boom_line()]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        assert len(results) >= 1
        pr = results[0]
        for field in _REQUIRED_FIELDS:
            assert hasattr(pr, field), f"PropertyResult missing field: {field!r}"

    def test_rentplus_vendor_field_set(self, rate_rules, property_master):
        from services.vendor_strategy.rentplus_strategy import RentPlusReconciliationStrategy

        strategy = RentPlusReconciliationStrategy()
        lines = [_make_rentplus_line()]
        vendor_ref = {"rate_rules": rate_rules, "property_aliases": []}

        results, _ = strategy.aggregate_and_rate_map(
            lines, property_master, vendor_ref, _REPORTING_MONTH
        )

        assert results[0].vendor == "Rent Plus"

    def test_boom_vendor_field_set(self):
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [_make_boom_line()]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        assert results[0].vendor == "Credit Boost powered by Boom"

    def test_transition_date_set_from_property_master(self):
        """transition_date must be populated from the property master."""
        from services.vendor_strategy.boom_strategy import BoomReconciliationStrategy

        strategy = BoomReconciliationStrategy()
        lines = [_make_boom_line()]
        pm = _make_boom_property_master()
        vendor_ref = _make_boom_vendor_ref()

        results, _ = strategy.aggregate_and_rate_map(
            lines, pm, vendor_ref, _REPORTING_MONTH
        )

        assert results[0].transition_date == date(2024, 6, 1)


# ---------------------------------------------------------------------------
# Test 7 – Vendor-specific parsing logic is isolated from Flask routes
# ---------------------------------------------------------------------------


class TestFlaskIsolation:
    """Strategy modules must not import Flask or route modules."""

    def test_strategies_work_without_flask_context(self):
        """Instantiating strategies must not require a Flask application context."""
        from services.vendor_strategy import get_vendor_strategy

        # If either strategy accidentally calls Flask internals, this will
        # raise RuntimeError("Working outside of application context.")
        rp = get_vendor_strategy("rent_plus")
        boom = get_vendor_strategy("credit_boost_boom")

        assert rp.vendor_code == "rent_plus"
        assert boom.vendor_code == "credit_boost_boom"
        assert rp.display_name == "Rent Plus"
        assert boom.display_name == "Credit Boost powered by Boom"

    def test_rentplus_strategy_source_has_no_flask_imports(self):
        import services.vendor_strategy.rentplus_strategy as mod

        src = inspect.getsource(mod)
        assert "from flask" not in src, "rentplus_strategy imports flask"
        assert "import flask" not in src, "rentplus_strategy imports flask"

    def test_boom_strategy_source_has_no_flask_imports(self):
        import services.vendor_strategy.boom_strategy as mod

        src = inspect.getsource(mod)
        assert "from flask" not in src, "boom_strategy imports flask"
        assert "import flask" not in src, "boom_strategy imports flask"

    def test_strategy_modules_do_not_import_routes(self):
        import services.vendor_strategy.rentplus_strategy as rp_mod
        import services.vendor_strategy.boom_strategy as boom_mod

        for mod in (rp_mod, boom_mod):
            src = inspect.getsource(mod)
            assert "from routes" not in src
            assert "import routes" not in src

    def test_factory_normalises_display_names(self):
        from services.vendor_strategy import get_vendor_strategy

        # Display names should resolve to the correct strategy
        assert get_vendor_strategy("Rent Plus").vendor_code == "rent_plus"
        assert (
            get_vendor_strategy("Credit Boost powered by Boom").vendor_code
            == "credit_boost_boom"
        )

    def test_unknown_vendor_raises_value_error(self):
        from services.vendor_strategy import get_vendor_strategy

        with pytest.raises(ValueError, match="Unknown vendor"):
            get_vendor_strategy("nonexistent_vendor_xyz")
