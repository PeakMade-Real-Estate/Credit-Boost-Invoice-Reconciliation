"""
End-to-end tests for the core reconciliation service.

These tests run the full engine against fixture CSV files
and verify the financial calculations and aggregate outputs.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from models.reconciliation_models import ReconciliationStatus
from services.reconciliation_service import run_reconciliation

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def run_result():
    """Run a full reconciliation against the sample fixture files."""
    return run_reconciliation(
        invoice_file_path=str(FIXTURES / "sample_invoice.csv"),
        cash_report_file_path=str(FIXTURES / "sample_cash_report.csv"),
        reporting_month="2026-07",
        vendor="Rent Plus",
        property_master_path=str(FIXTURES / "property_master.csv"),
        rate_mapping_path=str(FIXTURES / "rate_mapping.csv"),
        property_aliases_path=str(FIXTURES / "property_aliases.csv"),
        invoice_tolerance=Decimal("0.01"),
        balance_tolerance=Decimal("1.00"),  # relaxed for fixture data
        generate_outputs=False,
    )


class TestReconciliationService:
    def test_result_is_returned(self, run_result):
        assert run_result is not None

    def test_run_id_present(self, run_result):
        assert run_result.run_id

    def test_status_is_valid_enum(self, run_result):
        assert run_result.status in list(ReconciliationStatus)

    def test_invoice_summary_present(self, run_result):
        assert run_result.invoice_summary is not None

    def test_portfolio_totals_present(self, run_result):
        assert run_result.portfolio_totals is not None

    def test_property_results_populated(self, run_result):
        assert len(run_result.property_results) >= 1

    def test_invoice_lines_populated(self, run_result):
        assert len(run_result.invoice_lines) >= 1

    def test_cash_records_populated(self, run_result):
        assert len(run_result.cash_records) >= 1

    def test_no_errors(self, run_result):
        assert run_result.errors == []


class TestCashAggregation:
    """Test 13 – Cash is aggregated per property."""

    def test_cash_summed_per_property(self, run_result):
        """Each property result should have cash_received set."""
        for pr in run_result.property_results:
            # Cash should be non-negative for matched properties
            assert pr.cash_received >= Decimal("0")


class TestPropertyRevenueShare:
    """Test 14 – Actual property revenue share = cash - invoice owed."""

    def test_actual_revenue_share_formula(self, run_result):
        for pr in run_result.property_results:
            expected = pr.cash_received - pr.invoice_amount_owed
            assert pr.actual_property_revenue_share == expected


class TestPeakRevenueShare:
    """Test 15 – Expected Peak share is calculated from rate rules."""

    def test_peak_revenue_share_is_nonnegative_for_active_properties(self, run_result):
        for pr in run_result.property_results:
            if pr.net_policy_quantity > Decimal("0"):
                # For positive policy quantities, peak share should be positive
                assert pr.expected_peak_revenue_share >= Decimal("0")


class TestPortfolioBalance:
    """Test 16 – Portfolio balance = cash - owed - revenue share."""

    def test_portfolio_balance_formula(self, run_result):
        totals = run_result.portfolio_totals
        assert totals is not None
        expected_diff = (
            totals.total_cash_received
            - totals.total_invoice_amount_owed
            - totals.total_actual_property_revenue_share
        )
        assert totals.balance_difference == expected_diff
