"""Tests for invoice total and portfolio balance validations."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models.reconciliation_models import (
    ExceptionSeverity,
    InvoiceSummary,
    PortfolioTotals,
    PropertyResult,
    ReconciliationStatus,
)
from services.validation_service import run_all_validations


def _make_summary(source_total: str, calculated_total: str) -> InvoiceSummary:
    src = Decimal(source_total)
    calc = Decimal(calculated_total)
    diff = calc - src
    return InvoiceSummary(
        invoice_id="INV-TEST",
        vendor="Rent Plus",
        reporting_month="2026-07",
        source_invoice_total=src,
        calculated_invoice_total=calc,
        invoice_difference=diff,
        invoice_validation_status="passed" if abs(diff) < Decimal("0.02") else "failed",
        line_count=5,
        property_count=3,
        source_file_name="invoice.csv",
        source_file_hash="abc123",
    )


def _run(
    source_total="100.00",
    calculated_total="100.00",
    balance_diff="0.00",
    property_results=None,
    known_hashes=None,
    invoice_hash="abc123",
    cash_hash="def456",
):
    totals = PortfolioTotals(
        total_properties=3,
        total_net_policy_quantity=Decimal("10"),
        total_invoice_amount_owed=Decimal("100.00"),
        total_cash_received=Decimal("110.00"),
        total_actual_property_revenue_share=Decimal(balance_diff),
        total_expected_peak_revenue_share=Decimal("20.00"),
        balance_difference=Decimal(balance_diff),
        overall_status=ReconciliationStatus.PASSED,
    )
    return run_all_validations(
        invoice_lines=[],
        cash_records=[],
        property_results=property_results or [],
        invoice_summary=_make_summary(source_total, calculated_total),
        portfolio_totals=totals,
        reporting_month="2026-07",
        invoice_file_hash=invoice_hash,
        cash_file_hash=cash_hash,
        invoice_tolerance=Decimal("0.01"),
        balance_tolerance=Decimal("0.05"),
        known_hashes=known_hashes or [],
    )


class TestInvoiceTotalValidation:
    """Test 12 – Invoice total validation."""

    def test_matching_totals_pass(self):
        results = _run(source_total="545.45", calculated_total="545.45")
        check = next(r for r in results if r.check_name == "invoice_total_balance")
        assert check.passed

    def test_mismatching_totals_fail(self):
        results = _run(source_total="545.45", calculated_total="500.00")
        check = next(r for r in results if r.check_name == "invoice_total_balance")
        assert not check.passed
        assert check.severity == ExceptionSeverity.BLOCKING

    def test_tiny_difference_within_tolerance_passes(self):
        results = _run(source_total="545.45", calculated_total="545.454")
        check = next(r for r in results if r.check_name == "invoice_total_balance")
        assert check.passed


class TestDuplicateFileDetection:
    """Test 17 – Duplicate file detection."""

    def test_new_file_hash_passes(self):
        results = _run(invoice_hash="brand_new_hash", known_hashes=[])
        check = next(r for r in results if r.check_name == "duplicate_file_detection")
        assert check.passed

    def test_known_invoice_hash_fails(self):
        results = _run(invoice_hash="already_seen", known_hashes=["already_seen"])
        check = next(r for r in results if r.check_name == "duplicate_file_detection")
        assert not check.passed
        assert check.severity == ExceptionSeverity.BLOCKING


class TestPortfolioBalanceControl:
    """Test 16 – Portfolio balancing control."""

    def test_zero_balance_passes(self):
        results = _run(balance_diff="0.00")
        check = next(r for r in results if r.check_name == "portfolio_balance_control")
        assert check.passed

    def test_small_balance_within_tolerance_passes(self):
        results = _run(balance_diff="0.03")
        check = next(r for r in results if r.check_name == "portfolio_balance_control")
        assert check.passed

    def test_large_balance_fails(self):
        results = _run(balance_diff="10.00")
        check = next(r for r in results if r.check_name == "portfolio_balance_control")
        assert not check.passed


class TestDuplicatePropertyRecords:
    """Test: duplicate property records are detected."""

    def test_no_duplicates_passes(self):
        prs = [
            PropertyResult(
                internal_property_id="CB-0001",
                pms_property_id="PMS-101",
                vendor_property_id="RP-1001",
                property_name="A",
                reporting_month="2026-07",
            ),
            PropertyResult(
                internal_property_id="CB-0002",
                pms_property_id="PMS-102",
                vendor_property_id="RP-1002",
                property_name="B",
                reporting_month="2026-07",
            ),
        ]
        results = _run(property_results=prs)
        check = next(r for r in results if r.check_name == "duplicate_final_property_records")
        assert check.passed

    def test_duplicates_fail(self):
        prs = [
            PropertyResult(
                internal_property_id="CB-0001",
                pms_property_id="PMS-101",
                vendor_property_id="RP-1001",
                property_name="A",
                reporting_month="2026-07",
            ),
            PropertyResult(
                internal_property_id="CB-0001",  # duplicate!
                pms_property_id="PMS-101",
                vendor_property_id="RP-1001",
                property_name="A",
                reporting_month="2026-07",
            ),
        ]
        results = _run(property_results=prs)
        check = next(r for r in results if r.check_name == "duplicate_final_property_records")
        assert not check.passed
