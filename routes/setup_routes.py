"""
Setup routes — one-time configuration utilities.

Provides a "Seed Property Master" wizard that:
  1. Accepts an invoice PDF + Entrata cash report upload.
  2. Parses both files and fuzzy-matches property names across them.
  3. Renders a review table where the user can correct IDs and dates.
  4. Saves the approved rows to data/property_master.csv.

Routes
------
GET  /setup/property-master           — upload form
POST /setup/property-master/preview   — parse files, render review table
POST /setup/property-master/save      — write approved rows to CSV
"""
from __future__ import annotations

import csv
import logging
import os
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from services.cash_report_parser import parse_cash_report
from services.pdf_invoice_parser import parse_rent_plus_pdf
from services.property_master_loader import load_property_master
from services.property_master_seeder import seed_from_invoice_and_cash_report

logger = logging.getLogger(__name__)
bp = Blueprint("setup", __name__, url_prefix="/setup")

# Columns written to property_master.csv — order matters.
_PM_COLUMNS = [
    "internal_property_id",
    "pms_property_id",
    "vendor_property_id",
    "property_name",
    "normalized_property_name",
    "vendor",
    "program_status",
    "program_start_date",
    "program_end_date",
    "charge_code",
    "active",
]


# ---------------------------------------------------------------------------
# Upload form
# ---------------------------------------------------------------------------


@bp.route("/property-master", methods=["GET"])
def seed_property_master():
    """Render the seeder upload form."""
    vendors = current_app.config.get("SUPPORTED_VENDORS", ["Rent Plus"])
    return render_template("setup/seed_upload.html", vendors=vendors)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@bp.route("/property-master/preview", methods=["POST"])
def seed_property_master_preview():
    """Parse uploaded files and render the review table."""
    vendor = request.form.get("vendor", "Rent Plus").strip()
    try:
        fuzzy_threshold = max(0, min(100, int(request.form.get("fuzzy_threshold", 70))))
    except ValueError:
        fuzzy_threshold = 70

    invoice_file = request.files.get("invoice_file")
    cash_file = request.files.get("cash_report_file")

    if not invoice_file or not invoice_file.filename:
        flash("Please select an invoice file.", "danger")
        return redirect(url_for("setup.seed_property_master"))
    if not cash_file or not cash_file.filename:
        flash("Please select a cash report file.", "danger")
        return redirect(url_for("setup.seed_property_master"))

    inv_ext = Path(secure_filename(invoice_file.filename)).suffix.lower()
    if inv_ext != ".pdf":
        flash("The invoice must be a PDF file for the property master seeder.", "danger")
        return redirect(url_for("setup.seed_property_master"))

    cash_ext = Path(secure_filename(cash_file.filename)).suffix.lower()
    if cash_ext not in (".csv", ".xlsx"):
        flash("The cash report must be CSV or XLSX.", "danger")
        return redirect(url_for("setup.seed_property_master"))

    # Save uploads to a temporary seed folder
    seed_folder = Path(current_app.config["UPLOAD_FOLDER"]) / "seed"
    seed_folder.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    inv_path  = str(seed_folder / f"seed_{token}_invoice{inv_ext}")
    cash_path = str(seed_folder / f"seed_{token}_cash{cash_ext}")
    invoice_file.save(inv_path)
    cash_file.save(cash_path)

    try:
        # Parse invoice for property names
        invoice_lines, _ = parse_rent_plus_pdf(inv_path, reporting_month="0000-00")

        # Parse cash report for PMS IDs (no charge-code filter — want all properties)
        cash_records = parse_cash_report(cash_path, reporting_month="0000-00")

        # Load existing property master (best-effort)
        pm_path = Path(current_app.root_path) / "data" / "property_master.csv"
        try:
            existing_pm = load_property_master(str(pm_path))
        except (FileNotFoundError, ValueError):
            existing_pm = []

        proposed = seed_from_invoice_and_cash_report(
            invoice_lines,
            cash_records,
            existing_property_master=existing_pm,
            fuzzy_threshold=fuzzy_threshold,
            vendor=vendor,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Property master seed preview failed: %s", exc)
        flash(f"Error processing files: {exc}", "danger")
        return redirect(url_for("setup.seed_property_master"))
    finally:
        for p in (inv_path, cash_path):
            try:
                os.remove(p)
            except OSError:
                pass

    new_count = sum(1 for r in proposed if r["is_new"])
    matched_count = sum(1 for r in proposed if r["cash_match_confidence"] > 0)

    return render_template(
        "setup/seed_review.html",
        proposed=proposed,
        vendor=vendor,
        row_count=len(proposed),
        new_count=new_count,
        already_count=len(proposed) - new_count,
        matched_count=matched_count,
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


@bp.route("/property-master/save", methods=["POST"])
def seed_property_master_save():
    """Write the approved rows to data/property_master.csv."""
    row_count = int(request.form.get("row_count", 0))
    save_mode = request.form.get("save_mode", "replace")  # "replace" | "merge"

    # Collect rows the user checked
    new_rows: list[dict] = []
    for i in range(row_count):
        if request.form.get(f"row_{i}_include") != "on":
            continue
        new_rows.append({
            "internal_property_id":     request.form.get(f"row_{i}_internal_property_id", "").strip(),
            "pms_property_id":          request.form.get(f"row_{i}_pms_property_id", "").strip(),
            "vendor_property_id":       request.form.get(f"row_{i}_vendor_property_id", "").strip(),
            "property_name":            request.form.get(f"row_{i}_property_name", "").strip(),
            "normalized_property_name": request.form.get(f"row_{i}_normalized_property_name", "").strip(),
            "vendor":                   request.form.get(f"row_{i}_vendor", "").strip(),
            "program_status":           "active",
            "program_start_date":       request.form.get(f"row_{i}_program_start_date", "").strip(),
            "program_end_date":         "",
            "charge_code":              request.form.get(f"row_{i}_charge_code", "").strip(),
            "active":                   "true",
        })

    if not new_rows:
        flash("No rows were selected — nothing was saved.", "warning")
        return redirect(url_for("setup.seed_property_master"))

    pm_path = Path(current_app.root_path) / "data" / "property_master.csv"
    existing_rows: list[dict] = []

    if save_mode == "merge" and pm_path.exists():
        # Keep existing rows; skip any new rows whose property_name already exists
        existing_lower: set[str] = set()
        with open(pm_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing_rows.append(dict(row))
                existing_lower.add((row.get("property_name") or "").strip().lower())
        new_rows = [
            r for r in new_rows
            if r["property_name"].lower() not in existing_lower
        ]

    final_rows = existing_rows + new_rows
    pm_path.parent.mkdir(parents=True, exist_ok=True)

    with open(pm_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_PM_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(final_rows)

    verb = "Merged into" if save_mode == "merge" else "Replaced"
    flash(
        f"{verb} property master — {len(new_rows)} new "
        f"{'property' if len(new_rows) == 1 else 'properties'} added.  "
        f"Total records: {len(final_rows)}.",
        "success",
    )
    return redirect(url_for("reconciliation.upload"))
