"""
Excel output generator.

Produces two workbooks using ``openpyxl``:

  1. **Accounting Summary** – one row per property with portfolio summary.
  2. **Detailed Audit**     – one row per invoice line, plus exception sheets.

Usage::

    from services.output_generator import generate_outputs

    accounting_path, audit_path = generate_outputs(result, output_folder)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

from models.reconciliation_models import ExceptionType, ReconciliationResult
from services.utils import normalize_text

logger = logging.getLogger(__name__)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    _OPENPYXL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OPENPYXL_AVAILABLE = False
    logger.warning("openpyxl is not installed.  Excel output will not be generated.")

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
_HEADER_FILL = "1F3864"   # Dark navy
_HEADER_FONT = "FFFFFF"   # White
_ALT_ROW_FILL = "EBF3FB"  # Light blue
_WARNING_FILL = "FFF2CC"  # Yellow
_ERROR_FILL = "FCE4D6"    # Salmon
_OK_FILL = "E2EFDA"       # Light green
_SUMMARY_FILL = "D6E4F0"  # Light blue-grey

_CURRENCY_FMT = '#,##0.00'
_INTEGER_FMT = '#,##0'
_PCT_FMT = '0.00%'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_outputs(
    result: ReconciliationResult,
    output_folder: str,
) -> Tuple[str, str]:
    """Write both Excel workbooks and return their paths.

    Args:
        result:        The complete ``ReconciliationResult``.
        output_folder: Folder where files are written.

    Returns:
        Tuple of (accounting_summary_path, audit_path).
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError(
            "openpyxl is not installed.  "
            "Run: pip install openpyxl"
        )

    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_slug = result.run_id.replace(" ", "_")

    accounting_path = str(
        Path(output_folder) / f"{run_slug}_accounting_summary_{timestamp}.xlsx"
    )
    audit_path = str(
        Path(output_folder) / f"{run_slug}_detailed_audit_{timestamp}.xlsx"
    )

    _write_accounting_summary(result, accounting_path)
    _write_detailed_audit(result, audit_path)

    logger.info("Output files written: %s  %s", accounting_path, audit_path)
    return accounting_path, audit_path


def generate_reconciliation_workbook(
    result: ReconciliationResult,
    output_folder: str,
) -> str:
    """Write the workbook-style reconciliation XLSX and return its path.

    Columns: PROPERTY, QTY, AMOUNT, Cash Received, PA Rev Share,
             Transition Date, Notes — formatted as an Excel Table with
             auto-fitted column widths.

    Properties present in the cash report but absent from the invoice are
    included with "not in invoice" in QTY, AMOUNT, and PA Rev Share.
    A bolded TOTAL row is appended below the table.
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl")

    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_slug = result.run_id.replace(" ", "_")
    xlsx_path = str(Path(output_folder) / f"{run_slug}_reconciliation_{timestamp}.xlsx")

    cash_by_norm_name = {
        normalize_text(r.property_name): r
        for r in result.cash_records
        if r.property_name
    }

    _NOT_IN_INVOICE = "not in invoice"
    _HEADERS = [
        "PROPERTY", "QTY", "AMOUNT", "Cash Received",
        "PA Rev Share", "Transition Date", "Notes",
    ]

    rows = []

    for pr in result.property_results:
        rows.append([
            pr.property_name,
            int(pr.net_policy_quantity),
            round(float(pr.invoice_amount_owed), 2),
            round(float(pr.cash_received), 2),
            round(float(pr.actual_property_revenue_share), 2),
            "",
            "",
        ])

    invoice_norm_names = {normalize_text(pr.property_name) for pr in result.property_results}
    for exc in result.property_exceptions:
        if exc.exception_type != ExceptionType.NO_INVOICE_RECORD:
            continue
        prop_name = exc.invoice_property_name or ""
        norm = normalize_text(prop_name)
        if norm in invoice_norm_names:
            continue
        cash_rec = cash_by_norm_name.get(norm)
        cash_amount = round(float(cash_rec.cash_received), 2) if cash_rec else None
        rows.append([prop_name, _NOT_IN_INVOICE, _NOT_IN_INVOICE, cash_amount, _NOT_IN_INVOICE, "", ""])

    rows.sort(key=lambda r: str(r[0]).lower())

    def _col_sum(col_idx: int) -> float:
        return round(sum(r[col_idx] for r in rows if isinstance(r[col_idx], (int, float))), 2)

    total_row = [
        "TOTAL",
        sum(r[1] for r in rows if isinstance(r[1], int)),
        _col_sum(2),
        _col_sum(3),
        _col_sum(4),
        "",
        "",
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "Reconciliation"

    ws.append(_HEADERS)
    for row in rows:
        ws.append(row)

    # Table covers header + data rows (excludes total row)
    table_end_row = len(rows) + 1
    table_ref = f"A1:{get_column_letter(len(_HEADERS))}{table_end_row}"
    tbl = Table(displayName="ReconciliationTable", ref=table_ref)
    tbl.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(tbl)

    # Total row (below table so it isn't part of it)
    total_row_idx = table_end_row + 1
    ws.append(total_row)
    for col_idx in range(1, len(_HEADERS) + 1):
        cell = ws.cell(row=total_row_idx, column=col_idx)
        cell.font = Font(bold=True)

    # Number formats: col 2 = integer, cols 3-5 = currency
    _INT_FMT = "#,##0"
    _CUR_FMT = "#,##0.00"
    for row_idx in range(2, total_row_idx + 1):
        qty_cell = ws.cell(row=row_idx, column=2)
        if isinstance(qty_cell.value, int):
            qty_cell.number_format = _INT_FMT
        for col_idx in (3, 4, 5):
            cell = ws.cell(row=row_idx, column=col_idx)
            if isinstance(cell.value, float):
                cell.number_format = _CUR_FMT

    # Auto-fit column widths
    for col_cells in ws.columns:
        max_len = max(
            (len(str(cell.value)) if cell.value is not None else 0 for cell in col_cells),
            default=0,
        )
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(max_len + 4, 60)

    wb.save(xlsx_path)
    logger.info("Reconciliation workbook written: %s", xlsx_path)
    return xlsx_path


# ---------------------------------------------------------------------------
# Accounting summary workbook
# ---------------------------------------------------------------------------


def _write_accounting_summary(result: ReconciliationResult, path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Accounting Summary"

    # Core spec columns (same for every vendor profile)
    headers = [
        "Reporting Month",
        "Internal Property ID",
        "PMS Property ID",
        "Property",
        "QTY",
        "AMOUNT",
        "Cash Received",
        "PA Rev Share",
        "Transition Date",
        "Notes",
        "Vendor",
        "Validation Status",
        # Extended audit columns
        "Charge Code",
        "Legacy Policy Qty",
        "Standard Policy Qty",
        "Amount to Pull",
        "Expected Peak Rev Share",
        "Expected Owner Share",
        "Expected Resident Charges",
        "Collection Rate",
    ]

    _write_header_row(ws, headers, row=1)
    ws.freeze_panes = "A2"

    for row_idx, pr in enumerate(result.property_results, start=2):
        fill = _status_fill(pr.validation_status)
        row_data = [
            pr.reporting_month,
            pr.internal_property_id,
            pr.pms_property_id,
            pr.property_name,
            pr.net_policy_quantity,               # QTY
            pr.invoice_amount_owed,               # AMOUNT
            pr.cash_received,
            pr.actual_property_revenue_share,     # PA Rev Share
            pr.transition_date,                   # Transition Date
            pr.notes,
            pr.vendor,
            pr.validation_status,
            # Extended audit fields
            pr.charge_code,
            pr.legacy_policy_quantity,
            pr.standard_policy_quantity,
            pr.amount_to_pull,
            pr.expected_peak_revenue_share,
            pr.expected_owner_share,
            pr.expected_resident_charges,
            float(pr.collection_rate) if pr.collection_rate else None,
        ]
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=_safe_value(value))
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)

    # Apply number formats  (col positions match the new header order)
    currency_cols = [6, 7, 8, 16, 17, 18, 19]   # AMOUNT, Cash, PA Rev Share, pulls, shares
    int_cols = [5, 14, 15]                        # QTY, Legacy Qty, Standard Qty
    pct_cols = [20]                               # Collection Rate
    _apply_column_formats(ws, currency_cols, _CURRENCY_FMT, start_row=2)
    _apply_column_formats(ws, int_cols, _INTEGER_FMT, start_row=2)
    _apply_column_formats(ws, pct_cols, _PCT_FMT, start_row=2)

    # --- Portfolio summary section ---
    if result.portfolio_totals:
        totals = result.portfolio_totals
        summary_start = len(result.property_results) + 4
        _write_portfolio_summary(ws, totals, summary_start)

    _auto_column_widths(ws)
    wb.save(path)
    logger.info("Accounting summary written: %s", path)


def _write_portfolio_summary(ws, totals, start_row: int) -> None:
    from openpyxl.styles import Font, PatternFill

    labels = [
        ("Total Properties", totals.total_properties),
        ("Total Net Policy Qty", totals.total_net_policy_quantity),
        ("Total Invoice Amount", totals.total_invoice_amount_owed),
        ("Total Cash Received", totals.total_cash_received),
        ("Total Property Revenue Share", totals.total_actual_property_revenue_share),
        ("Total Expected Peak Revenue Share", totals.total_expected_peak_revenue_share),
        ("Balance Difference", totals.balance_difference),
        ("Overall Status", totals.overall_status.value if totals.overall_status else ""),
    ]

    title_cell = ws.cell(row=start_row, column=1, value="PORTFOLIO SUMMARY")
    title_cell.font = Font(bold=True, size=12)
    title_cell.fill = PatternFill("solid", fgColor=_SUMMARY_FILL)

    for offset, (label, value) in enumerate(labels, start=1):
        label_cell = ws.cell(row=start_row + offset, column=1, value=label)
        label_cell.font = Font(bold=True)
        value_cell = ws.cell(
            row=start_row + offset, column=2, value=_safe_value(value)
        )
        if isinstance(value, Decimal):
            value_cell.number_format = _CURRENCY_FMT


# ---------------------------------------------------------------------------
# Detailed audit workbook
# ---------------------------------------------------------------------------


def _write_detailed_audit(result: ReconciliationResult, path: str) -> None:
    wb = Workbook()

    # Sheet 1 – Invoice lines
    ws_lines = wb.active
    ws_lines.title = "Invoice Lines"
    _write_invoice_lines_sheet(ws_lines, result)

    # Sheet 2 – Property exceptions
    ws_prop_exc = wb.create_sheet("Property Exceptions")
    _write_property_exceptions_sheet(ws_prop_exc, result)

    # Sheet 3 – Rate exceptions
    ws_rate_exc = wb.create_sheet("Rate Exceptions")
    _write_rate_exceptions_sheet(ws_rate_exc, result)

    # Sheet 4 – Validation results
    ws_val = wb.create_sheet("Validation Results")
    _write_validation_sheet(ws_val, result)

    # Sheet 5 – Source file metadata
    ws_meta = wb.create_sheet("Run Metadata")
    _write_metadata_sheet(ws_meta, result)

    wb.save(path)
    logger.info("Detailed audit written: %s", path)


def _write_invoice_lines_sheet(ws, result: ReconciliationResult) -> None:
    headers = [
        "Invoice ID", "Source Row", "Original Description", "Normalized Property",
        "Matched Property ID", "PMS Property ID", "Vendor Property ID",
        "Original Quantity", "Unit Price", "Line Amount",
        "Rate Rule ID", "Rate Name", "Multiplier", "Sign",
        "Normalized Policy Qty", "Match Status",
        "Exception Type", "Exception Reason",
        "Manual Override", "Override User", "Override Date",
    ]
    _write_header_row(ws, headers, row=1)
    ws.freeze_panes = "A2"

    for row_idx, line in enumerate(result.invoice_lines, start=2):
        row_data = [
            line.invoice_id,
            line.source_row,
            line.original_description,
            line.normalized_property_name,
            line.internal_property_id or "",
            line.pms_property_id or "",
            line.vendor_property_id,
            line.original_quantity,
            line.unit_price,
            line.line_amount,
            line.rate_rule_id or "",
            line.rate_type or "",
            line.policy_multiplier,
            line.sign,
            line.normalized_policy_quantity,
            line.match_status.value,
            "",  # Exception type (populated from property_exceptions)
            line.exception_reason or "",
            "Yes" if line.manual_override else "No",
            line.override_user or "",
            line.override_date.isoformat() if line.override_date else "",
        ]
        fill_colour = (
            _ERROR_FILL if line.match_status.value in ("unmatched", "fuzzy_suggestion")
            else _ALT_ROW_FILL if row_idx % 2 == 0
            else None
        )
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=_safe_value(value))
            if fill_colour:
                cell.fill = PatternFill("solid", fgColor=fill_colour)

    _apply_column_formats(ws, [8, 9, 10, 15], _CURRENCY_FMT, start_row=2)
    _auto_column_widths(ws)


def _write_property_exceptions_sheet(ws, result: ReconciliationResult) -> None:
    headers = [
        "Exception Type", "Severity", "Invoice Property Name",
        "Invoice Vendor Property ID", "Suggested Property ID",
        "Suggested Property Name", "Confidence", "Requires Review",
        "Message", "Resolved", "Resolution Note",
    ]
    _write_header_row(ws, headers, row=1)
    for row_idx, exc in enumerate(result.property_exceptions, start=2):
        row_data = [
            exc.exception_type.value,
            exc.severity.value,
            exc.invoice_property_name or "",
            exc.invoice_vendor_property_id or "",
            exc.suggested_property_id or "",
            exc.suggested_property_name or "",
            exc.confidence,
            "Yes" if exc.requires_user_review else "No",
            exc.message,
            "Yes" if exc.resolved else "No",
            exc.resolution_note or "",
        ]
        for col_idx, value in enumerate(row_data, start=1):
            ws.cell(row=row_idx, column=col_idx, value=_safe_value(value))
    _auto_column_widths(ws)


def _write_rate_exceptions_sheet(ws, result: ReconciliationResult) -> None:
    headers = [
        "Exception Type", "Severity", "Property Name",
        "Invoice Unit Price", "Source Row", "Message",
        "Suggested Rate Rule ID", "Resolved", "Resolution Note",
    ]
    _write_header_row(ws, headers, row=1)
    for row_idx, exc in enumerate(result.rate_exceptions, start=2):
        row_data = [
            exc.exception_type.value,
            exc.severity.value,
            exc.invoice_property_name or "",
            exc.invoice_unit_price,
            exc.source_row,
            exc.message,
            exc.suggested_rate_rule_id or "",
            "Yes" if exc.resolved else "No",
            exc.resolution_note or "",
        ]
        for col_idx, value in enumerate(row_data, start=1):
            ws.cell(row=row_idx, column=col_idx, value=_safe_value(value))
    _apply_column_formats(ws, [4], _CURRENCY_FMT, start_row=2)
    _auto_column_widths(ws)


def _write_validation_sheet(ws, result: ReconciliationResult) -> None:
    headers = [
        "Check Name", "Passed", "Severity",
        "Expected Value", "Actual Value", "Difference", "Message",
    ]
    _write_header_row(ws, headers, row=1)
    for row_idx, v in enumerate(result.validation_results, start=2):
        fill = _OK_FILL if v.passed else (
            _ERROR_FILL if v.severity.value == "BLOCKING" else _WARNING_FILL
        )
        row_data = [
            v.check_name,
            "PASS" if v.passed else "FAIL",
            v.severity.value,
            str(v.expected_value) if v.expected_value is not None else "",
            str(v.actual_value) if v.actual_value is not None else "",
            str(v.difference) if v.difference is not None else "",
            v.message,
        ]
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.fill = PatternFill("solid", fgColor=fill)
    _auto_column_widths(ws)


def _write_metadata_sheet(ws, result: ReconciliationResult) -> None:
    rows = [
        ("Run ID", result.run_id),
        ("Reporting Month", result.reporting_month),
        ("Vendor", result.vendor),
        ("Status", result.status.value),
        ("Invoice File", result.invoice_summary.source_file_name if result.invoice_summary else ""),
        ("Invoice File Hash", result.invoice_summary.source_file_hash if result.invoice_summary else ""),
        ("Generated At", datetime.utcnow().isoformat()),
        ("Total Properties", result.portfolio_totals.total_properties if result.portfolio_totals else 0),
        ("Total Exceptions", result.exception_count),
        ("Blocking Exceptions", result.blocking_exception_count),
    ]
    for row_idx, (label, value) in enumerate(rows, start=1):
        ws.cell(row=row_idx, column=1, value=label).font = Font(bold=True)
        ws.cell(row=row_idx, column=2, value=str(value))
    _auto_column_widths(ws)


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------


def _write_header_row(ws, headers: list, row: int = 1) -> None:
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=header)
        cell.font = Font(bold=True, color=_HEADER_FONT)
        cell.fill = PatternFill("solid", fgColor=_HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)


def _apply_column_formats(
    ws, col_indices: list, fmt: str, start_row: int = 2
) -> None:
    for col_idx in col_indices:
        for row in ws.iter_rows(
            min_row=start_row, max_row=ws.max_row, min_col=col_idx, max_col=col_idx
        ):
            for cell in row:
                cell.number_format = fmt


def _auto_column_widths(ws, max_width: int = 50) -> None:
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 4, max_width)


def _status_fill(status: str) -> Optional[str]:
    if status == "exception":
        return _ERROR_FILL
    if status == "warning":
        return _WARNING_FILL
    if status == "ok":
        return None  # No fill for OK rows
    return None


def _safe_value(value):
    """Convert Decimal/Enum to native Python type for openpyxl."""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "value"):  # Enum
        return value.value
    return value


# ---------------------------------------------------------------------------
# Resident Charges reconciliation workbook
# ---------------------------------------------------------------------------


def generate_resident_charges_workbook(
    result_data: dict,
    output_folder: str,
    run_id: str,
) -> str:
    """Write a Resident Charges reconciliation XLSX and return its path.

    Produces two sheets:
      1. **Summary** – one row per property with charged / billed / matched counts.
      2. **Discrepancies** – one row per discrepancy (charged-not-billed and
         billed-not-charged), with type, property, unit, resident, and amounts.

    Args:
        result_data:   The result dict loaded from the stored JSON.
        output_folder: Folder where the file is written.
        run_id:        Run identifier used in the filename.

    Returns:
        Absolute path to the generated XLSX file.
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl")

    from openpyxl.worksheet.table import Table, TableStyleInfo

    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    xlsx_path = str(
        Path(output_folder) / f"{run_id}_resident_charges_reconciliation_{timestamp}.xlsx"
    )

    wb = Workbook()

    # ------------------------------------------------------------------
    # Sheet 1 – Summary
    # ------------------------------------------------------------------
    ws_summary = wb.active
    ws_summary.title = "Summary"

    hdr_fill = PatternFill("solid", fgColor=_HEADER_FILL)
    hdr_font = Font(color=_HEADER_FONT, bold=True)
    alt_fill = PatternFill("solid", fgColor=_ALT_ROW_FILL)
    warn_fill = PatternFill("solid", fgColor=_WARNING_FILL)
    ok_fill = PatternFill("solid", fgColor=_OK_FILL)
    bold_font = Font(bold=True)
    currency_fmt = _CURRENCY_FMT

    summary_headers = [
        "Property",
        "Residents Charged",
        "Residents Billed",
        "Matched",
        "Charged Not Billed",
        "Billed Not Charged",
        "Total Charged ($)",
        "Total Billed ($)",
    ]

    ws_summary.append(summary_headers)
    for cell in ws_summary[1]:
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    summaries = result_data.get("property_summaries", [])
    for idx, s in enumerate(summaries, start=2):
        has_discrepancy = (
            s.get("charged_not_billed_count", 0) > 0
            or s.get("billed_not_charged_count", 0) > 0
        )
        row = [
            s.get("property_name", ""),
            s.get("residents_charged", 0),
            s.get("residents_billed", 0),
            s.get("matched_count", 0),
            s.get("charged_not_billed_count", 0),
            s.get("billed_not_charged_count", 0),
            float(s.get("total_charged_amount", 0) or 0),
            float(s.get("total_billed_amount", 0) or 0),
        ]
        ws_summary.append(row)
        row_fill = warn_fill if has_discrepancy else (alt_fill if idx % 2 == 0 else None)
        if row_fill:
            for cell in ws_summary[idx]:
                cell.fill = row_fill
        # Format currency columns
        for col in (7, 8):
            ws_summary.cell(row=idx, column=col).number_format = currency_fmt

    # Total row
    total_row_idx = len(summaries) + 2
    totals = [
        "TOTAL",
        sum(s.get("residents_charged", 0) for s in summaries),
        sum(s.get("residents_billed", 0) for s in summaries),
        sum(s.get("matched_count", 0) for s in summaries),
        sum(s.get("charged_not_billed_count", 0) for s in summaries),
        sum(s.get("billed_not_charged_count", 0) for s in summaries),
        round(sum(float(s.get("total_charged_amount", 0) or 0) for s in summaries), 2),
        round(sum(float(s.get("total_billed_amount", 0) or 0) for s in summaries), 2),
    ]
    ws_summary.append(totals)
    for cell in ws_summary[total_row_idx]:
        cell.font = bold_font
        cell.fill = PatternFill("solid", fgColor=_SUMMARY_FILL)
    for col in (7, 8):
        ws_summary.cell(row=total_row_idx, column=col).number_format = currency_fmt

    # Auto-fit columns
    for col_cells in ws_summary.columns:
        max_len = max(len(str(c.value or "")) for c in col_cells)
        ws_summary.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 40)

    # ------------------------------------------------------------------
    # Sheet 2 – Discrepancies
    # ------------------------------------------------------------------
    ws_disc = wb.create_sheet("Discrepancies")

    disc_headers = [
        "Type",
        "Property",
        "Unit",
        "Resident Name",
        "Lease Status",
        "Charged Amount ($)",
        "Billed Amount ($)",
    ]

    ws_disc.append(disc_headers)
    for cell in ws_disc[1]:
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    _TYPE_LABELS = {
        "charged_not_billed": "Charged, Not Billed",
        "billed_not_charged": "Billed, Not Charged",
    }
    _TYPE_FILLS = {
        "charged_not_billed": PatternFill("solid", fgColor=_WARNING_FILL),
        "billed_not_charged": PatternFill("solid", fgColor=_ERROR_FILL),
    }

    discrepancies = result_data.get("discrepancies", [])
    for idx, d in enumerate(discrepancies, start=2):
        dtype = d.get("discrepancy_type", "")
        row = [
            _TYPE_LABELS.get(dtype, dtype),
            d.get("property_name", ""),
            d.get("unit", ""),
            d.get("resident_name", ""),
            d.get("lease_status", ""),
            float(d.get("charged_amount", 0) or 0),
            float(d.get("billed_amount", 0) or 0),
        ]
        ws_disc.append(row)
        row_fill = _TYPE_FILLS.get(dtype)
        if row_fill:
            for cell in ws_disc[idx]:
                cell.fill = row_fill
        for col in (6, 7):
            ws_disc.cell(row=idx, column=col).number_format = currency_fmt

    # Auto-fit columns
    for col_cells in ws_disc.columns:
        max_len = max(len(str(c.value or "")) for c in col_cells)
        ws_disc.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 50)

    wb.save(xlsx_path)
    logger.info("Resident charges workbook written: %s", xlsx_path)
    return xlsx_path

