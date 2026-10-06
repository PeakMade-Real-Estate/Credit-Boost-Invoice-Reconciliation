"""Tests for the Accounting Summary 'Revenue Share Split' column."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models.reconciliation_models import (
    PortfolioTotals,
    PropertyResult,
    ReconciliationResult,
    ReconciliationStatus,
)
from services.output_generator import _write_accounting_summary

openpyxl = pytest.importorskip("openpyxl")


def _make_property_result(name: str, revenue_share: Decimal) -> PropertyResult:
    return PropertyResult(
        internal_property_id=f"CB-{name}",
        pms_property_id="",
        vendor_property_id="",
        property_name=name,
        reporting_month="2026-08",
        actual_property_revenue_share=revenue_share,
        validation_status="ok",
    )


@pytest.fixture
def result():
    property_results = [
        _make_property_result("555 Boulevard", Decimal("100.00")),  # Denali
        _make_property_result("Oak Tree Commons", Decimal("50.00")),  # Everest Campus (default)
        _make_property_result("CA Property", Decimal("999.00")),  # California -> excluded
    ]
    property_results[-1].net_policy_quantity = Decimal("5")
    return ReconciliationResult(
        status=ReconciliationStatus.PASSED,
        reporting_month="2026-08",
        vendor="Credit Boost powered by Boom",
        run_id="REC-TEST",
        property_results=property_results,
        portfolio_totals=PortfolioTotals(
            total_properties=3,
            total_actual_property_revenue_share=Decimal("1149.00"),
            total_invoice_amount_owed=Decimal("500.00"),
            total_cash_received=Decimal("1649.00"),
            total_net_policy_quantity=Decimal("5"),
            balance_difference=Decimal("0.00"),
        ),
    )


@pytest.fixture(autouse=True)
def _mock_property_states(monkeypatch):
    """Never hit the live SharePoint backend; 'CA Property' resolves to CA."""
    monkeypatch.setattr(
        "services.property_state_service.get_property_states",
        lambda force_refresh=False: {"ca property": "CA"},
    )


def test_revenue_share_split_column_and_group_totals(result, tmp_path):
    out_path = tmp_path / "accounting_summary.xlsx"
    _write_accounting_summary(result, str(out_path))

    wb = openpyxl.load_workbook(out_path)
    ws = wb["Accounting Summary"]

    headers = [cell.value for cell in ws[1]]
    assert "Revenue Share Split" in headers
    split_col = headers.index("Revenue Share Split") + 1

    # Existing property list is unaffected (still exactly 3 data rows).
    data_rows = [row for row in ws.iter_rows(min_row=2, max_row=4, values_only=False)]
    assert len(data_rows) == 3

    groups_by_property = {
        row[headers.index("Property")].value: row[split_col - 1].value
        for row in data_rows
    }
    assert groups_by_property["555 Boulevard"] == "Denali"
    assert groups_by_property["Oak Tree Commons"] == "Everest Campus"
    assert groups_by_property["CA Property"] == "Everest Campus"

    # Group subtotal rows appear in the portfolio summary block.
    summary_labels = [row[0].value for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value]
    assert "Denali Revenue Share Total" in summary_labels
    assert "Everest Campus Revenue Share Total" in summary_labels

    label_to_row = {row[0].value: row[0].row for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value}
    denali_total = ws.cell(row=label_to_row["Denali Revenue Share Total"], column=2).value
    everest_total = ws.cell(row=label_to_row["Everest Campus Revenue Share Total"], column=2).value
    assert denali_total == pytest.approx(100.00)
    # Everest Campus total excludes the CA property's $999 revenue share.
    assert everest_total == pytest.approx(50.00)

    # Notes footer: CA property's QTY (5) is excluded from the peak-share benchmark too.
    peak_share_row = label_to_row["Peak revenue share from Credit Boost @ $3/bed"]
    assert ws.cell(row=peak_share_row, column=2).value == pytest.approx(0.0)

    assert ws.cell(row=label_to_row["Invoice from Credit Boost"], column=2).value == pytest.approx(500.00)
    assert ws.cell(row=label_to_row["Property rev share"], column=2).value == pytest.approx(1149.00)
    assert ws.cell(row=label_to_row["Total collected"], column=2).value == pytest.approx(1649.00)
    assert ws.cell(row=label_to_row["s/b zero"], column=2).value == pytest.approx(0.0)

