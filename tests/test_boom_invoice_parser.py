"""Tests for parse_boom_file's handling of re-uploaded Redpoint invoices.

Regression coverage for: a user downloads a generated Redpoint invoice,
deletes rows for properties that should be excluded (e.g. not in a pilot),
then re-uploads that edited file as the vendor invoice for reconciliation.
The hidden 'Reconciliation Data' sheet always retains every original raw
row (required so qualifying-filter columns survive a re-upload), so those
row deletions must be applied against it too rather than silently ignored.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from services.boom_invoice_parser import parse_boom_file

_RAW_COLUMNS = [
    "ID", "Property Name", "Category", "Transaction Type",
    "Template Name", "Subject Type", "Amount",
]

_RAW_ROWS = [
    ("TXN-1", "Beach Club", "partner_boom_report_ongoing_fee", "Invoice", "BoomReport", "Boom::ReportingAccount", "6.50"),
    ("TXN-2", "Beach Club", "partner_boom_report_ongoing_fee", "Invoice", "BoomReport", "Boom::ReportingAccount", "6.50"),
    ("TXN-3", "Campus Creek", "partner_boom_report_ongoing_fee", "Invoice", "BoomReport", "Boom::ReportingAccount", "6.50"),
]


def _build_redpoint_invoice_xlsx(path: Path, *, kept_properties: set[str]) -> None:
    """Build a 3-sheet workbook mimicking generate_redpoint_invoice's output,
    with 'Property Summary'/'Redpoint Invoice' edited down to kept_properties
    while the hidden 'Reconciliation Data' sheet keeps every raw row."""
    wb = Workbook()

    summary_ws = wb.active
    summary_ws.title = "Property Summary"
    summary_ws.append([None])  # rows 1-9 are logo/title rows in the real file
    for _ in range(8):
        summary_ws.append([None])
    summary_ws.append(["Property", "People Count", "Total Amount"])  # row 10
    for prop in sorted(kept_properties):
        summary_ws.append([prop, 1, 6.50])

    invoice_ws = wb.create_sheet("Redpoint Invoice")
    for _ in range(7):
        invoice_ws.append([None])
    invoice_ws.append(["Property Name", "Amount"])  # row 8
    for row in _RAW_ROWS:
        if row[1] in kept_properties:
            invoice_ws.append([row[1], row[6]])

    recon_ws = wb.create_sheet("Reconciliation Data")
    recon_ws.append(_RAW_COLUMNS)
    for row in _RAW_ROWS:
        recon_ws.append(list(row))

    wb.save(path)


def test_deleted_properties_in_visible_sheets_are_excluded(tmp_path):
    xlsx_path = tmp_path / "redpoint_invoice.xlsx"
    _build_redpoint_invoice_xlsx(xlsx_path, kept_properties={"Beach Club"})

    lines, summary = parse_boom_file(str(xlsx_path), reporting_month="2026-08")

    assert {l.boom_property_id for l in lines} == {"Beach Club"}
    assert summary.line_count == 2  # only Beach Club's 2 transactions


def test_no_edits_keeps_all_properties(tmp_path):
    xlsx_path = tmp_path / "redpoint_invoice.xlsx"
    _build_redpoint_invoice_xlsx(xlsx_path, kept_properties={"Beach Club", "Campus Creek"})

    lines, summary = parse_boom_file(str(xlsx_path), reporting_month="2026-08")

    assert {l.boom_property_id for l in lines} == {"Beach Club", "Campus Creek"}
    assert summary.line_count == 3
