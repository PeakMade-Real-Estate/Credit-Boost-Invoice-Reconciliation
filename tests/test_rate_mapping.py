"""Tests for the rate mapping engine."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models.reconciliation_models import ExceptionType, MatchStatus
from services.rate_mapping import apply_rate_mapping
from tests.conftest import make_invoice_line


class TestStandardSingleRate:
    """Test 7 – Standard single policy (7.85)."""

    def test_single_rate_quantity(self, rate_rules):
        line = make_invoice_line(quantity="4", unit_price="7.85", amount="31.40")
        line.internal_property_id = "CB-0001"
        lines, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        assert lines[0].rate_rule_id == "RR-001"
        assert lines[0].policy_multiplier == Decimal("1")
        assert lines[0].sign == Decimal("1")
        # normalized_policy_quantity = 4 * 1 * 1 = 4
        assert lines[0].normalized_policy_quantity == Decimal("4")
        assert not any(e.exception_type == ExceptionType.UNKNOWN_RATE for e in exceptions)


class TestStandardDoubleRate:
    """Test 8 – Standard double policy (15.70 = 2 policies)."""

    def test_double_rate_quantity(self, rate_rules):
        line = make_invoice_line(quantity="4", unit_price="15.70", amount="62.80")
        line.internal_property_id = "CB-0001"
        lines, _ = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        assert lines[0].rate_rule_id == "RR-002"
        # normalized_policy_quantity = 4 * 2 * 1 = 8
        assert lines[0].normalized_policy_quantity == Decimal("8")


class TestNegativeReversalRate:
    """Test 9 – Negative reversal rate."""

    def test_reversal_quantity_is_negative(self, rate_rules):
        line = make_invoice_line(
            quantity="1",
            unit_price="-7.85",
            amount="-7.85",
        )
        line.internal_property_id = "CB-0001"
        lines, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        assert lines[0].rate_rule_id == "RR-005"
        assert lines[0].sign == Decimal("-1")
        # normalized_policy_quantity = 1 * 1 * -1 = -1
        assert lines[0].normalized_policy_quantity == Decimal("-1")


class TestPropertySpecificLegacyRate:
    """Test 10 – Property-specific legacy rate (CB-0003, 6.50)."""

    def test_legacy_rate_matched_for_specific_property(self, rate_rules):
        line = make_invoice_line(quantity="3", unit_price="6.50", amount="19.50")
        line.internal_property_id = "CB-0003"  # the property with legacy rate
        lines, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        assert lines[0].rate_rule_id == "RR-007"
        assert "legacy" in lines[0].rate_type.lower()
        # Legacy warning should be raised
        legacy_warnings = [
            e for e in exceptions
            if e.exception_type == ExceptionType.LEGACY_RATE_ACTIVE
        ]
        assert legacy_warnings

    def test_legacy_rate_not_matched_for_other_property(self, rate_rules):
        """$6.50 for a non-CB-0003 property should raise UNKNOWN_RATE."""
        line = make_invoice_line(quantity="2", unit_price="6.50", amount="13.00")
        line.internal_property_id = "CB-0001"  # different property
        lines, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        unknown = [
            e for e in exceptions if e.exception_type == ExceptionType.UNKNOWN_RATE
        ]
        assert unknown


class TestUnknownRateBlockingException:
    """Test 11 – Unknown rate creates BLOCKING exception."""

    def test_unknown_rate_is_blocking(self, rate_rules):
        line = make_invoice_line(quantity="1", unit_price="99.99", amount="99.99")
        line.internal_property_id = "CB-0001"
        _, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        from models.reconciliation_models import ExceptionSeverity
        blocking = [
            e for e in exceptions
            if e.exception_type == ExceptionType.UNKNOWN_RATE
            and e.severity == ExceptionSeverity.BLOCKING
        ]
        assert blocking

    def test_unknown_rate_leaves_rate_rule_id_none(self, rate_rules):
        line = make_invoice_line(quantity="1", unit_price="99.99", amount="99.99")
        line.internal_property_id = "CB-0001"
        lines, _ = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
        )
        assert lines[0].rate_rule_id is None
        assert lines[0].normalized_policy_quantity is None


class TestManualRateOverride:
    """Rate overrides from the exceptions page are applied."""

    def test_manual_override_assigns_rule(self, rate_rules):
        line = make_invoice_line(quantity="2", unit_price="99.99", amount="199.98")
        line.internal_property_id = "CB-0001"
        line.source_row = 5
        lines, exceptions = apply_rate_mapping(
            invoice_lines=[line],
            rate_rules=rate_rules,
            reporting_month="2026-07",
            manual_rate_overrides={5: "RR-001"},
        )
        assert lines[0].rate_rule_id == "RR-001"
        assert lines[0].manual_override is True
