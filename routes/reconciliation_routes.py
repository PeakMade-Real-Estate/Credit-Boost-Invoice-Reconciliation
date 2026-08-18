"""
Flask route handlers for the invoice reconciliation web application.

These routes are responsible only for:
  - Receiving HTTP requests
  - File upload handling (secure_filename, type/size checks)
  - Calling the reusable reconciliation engine
  - Storing run metadata
  - Rendering templates with results
  - Serving download files

No reconciliation business logic lives in this file.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from models.reconciliation_models import (
    ManualOverride,
    ReconciliationRun,
    ReconciliationStatus,
)
from models.schemas import result_to_json
from services import run_reconciliation
from services.database import (
    delete_run,
    get_known_hashes,
    get_overrides,
    get_run,
    list_runs,
    get_next_sequence,
    register_file_hash,
    save_overrides,
    save_run,
    update_run_outputs,
)
from services.output_generator import generate_outputs, generate_reconciliation_workbook

logger = logging.getLogger(__name__)
bp = Blueprint("reconciliation", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext in current_app.config.get("ALLOWED_EXTENSIONS", {"csv", "xlsx"})


def _save_upload(file_storage, subfolder: str) -> tuple[str, str]:
    """Save an uploaded FileStorage to disk with a random name.

    Returns:
        Tuple of (stored_absolute_path, original_filename).
    """
    original_name = secure_filename(file_storage.filename)
    ext = Path(original_name).suffix.lower()
    random_name = f"{uuid.uuid4().hex}{ext}"
    folder = Path(current_app.config["UPLOAD_FOLDER"]) / subfolder
    folder.mkdir(parents=True, exist_ok=True)
    stored_path = str(folder / random_name)
    file_storage.save(stored_path)
    return stored_path, original_name


def _load_result(run_id: str) -> dict | None:
    """Load a serialised result dict for a run, or None if not found."""
    run_meta = get_run(run_id)
    if not run_meta:
        return None
    json_path = run_meta.get("result_json_path", "")
    if json_path and Path(json_path).exists():
        with open(json_path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _save_result_json(result, run_id: str) -> str:
    """Write the ReconciliationResult to a JSON file and return the path."""
    output_folder = current_app.config["OUTPUT_FOLDER"]
    os.makedirs(output_folder, exist_ok=True)
    json_path = str(Path(output_folder) / f"{run_id}_result.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(result_to_json(result))
    return json_path


# ---------------------------------------------------------------------------
# Home / upload
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
def index():
    """Landing page – redirects to the upload form."""
    return redirect(url_for("reconciliation.upload"))


@bp.route("/upload", methods=["GET"])
def upload():
    """Render the file upload form."""
    vendors = current_app.config.get("SUPPORTED_VENDORS", ["Rent Plus"])
    recent_runs = list_runs(limit=10)
    return render_template("upload.html", vendors=vendors, recent_runs=recent_runs)


@bp.route("/runs/<run_id>/delete", methods=["POST"])
def delete_run_route(run_id: str):
    """Delete a reconciliation run and all associated files."""
    run_meta = get_run(run_id)
    if run_meta is None:
        abort(404)
    deleted = delete_run(run_id)
    n_files = len(deleted["files_removed"])
    flash(
        f"Run {run_id} deleted ({n_files} file(s) removed).",
        "success",
    )
    return redirect(url_for("reconciliation.upload"))


@bp.route("/upload", methods=["POST"])
def process_upload():
    """Handle the upload form submission, run reconciliation, redirect."""
    import sys
    try:
        import yaml as _yaml_check
        _yaml_ok = f"yaml {_yaml_check.__version__} OK"
    except ImportError as _e:
        _yaml_ok = f"MISSING: {_e}"
    logger.info(
        "DIAG | python=%s | yaml=%s | sys.path=%s",
        sys.executable,
        _yaml_ok,
        sys.path,
    )

    reporting_month = request.form.get("reporting_month", "").strip()
    vendor = request.form.get("vendor", "").strip()

    # --- Input validation ---
    errors = []
    if not reporting_month:
        errors.append("Reporting month is required.")
    if not vendor:
        errors.append("Vendor is required.")

    invoice_file = request.files.get("invoice_file")
    cash_file = request.files.get("cash_report_file")

    if not invoice_file or invoice_file.filename == "":
        errors.append("Invoice file is required.")
    elif not _allowed_file(invoice_file.filename):
        errors.append("Invoice file must be CSV or XLSX.")

    if not cash_file or cash_file.filename == "":
        errors.append("Cash report file is required.")
    elif not _allowed_file(cash_file.filename):
        errors.append("Cash report file must be CSV or XLSX.")

    if errors:
        for err in errors:
            flash(err, "danger")
        return redirect(url_for("reconciliation.upload"))

    # --- Save files ---
    invoice_path, invoice_name = _save_upload(invoice_file, "invoices")
    cash_path, cash_name = _save_upload(cash_file, "cash_reports")

    # --- Generate run ID ---
    seq = get_next_sequence(reporting_month, vendor)
    run_id = ReconciliationRun.generate_run_id(reporting_month, vendor, seq)
    session["run_id"] = run_id

    # --- Retrieve known hashes for duplicate check ---
    known_hashes = get_known_hashes()

    # --- Run reconciliation ---
    try:
        result = run_reconciliation(
            invoice_file_path=invoice_path,
            cash_report_file_path=cash_path,
            reporting_month=reporting_month,
            vendor=vendor,
            property_master_path=current_app.config["PROPERTY_MASTER_PATH"],
            rate_mapping_path=current_app.config.get("RATE_MAPPING_PATH"),
            property_aliases_path=current_app.config.get("PROPERTY_ALIASES_PATH"),
            property_rollups_path=current_app.config.get("PROPERTY_ROLLUPS_PATH"),
            invoice_tolerance=current_app.config.get("INVOICE_TOLERANCE", Decimal("0.01")),
            balance_tolerance=current_app.config.get("BALANCE_TOLERANCE", Decimal("0.05")),
            fuzzy_threshold=current_app.config.get("FUZZY_MATCH_THRESHOLD", 80),
            known_file_hashes=known_hashes,
            uploaded_by=session.get("user", "web_user"),
            run_id=run_id,
            generate_outputs=False,  # Generate only when user requests download
            output_folder=current_app.config["OUTPUT_FOLDER"],
        )
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.error("Reconciliation error for run %s: %s\n%s", run_id, exc, tb)
        flash(f"Reconciliation failed: {exc!r} — see server log for traceback", "danger")
        return redirect(url_for("reconciliation.upload"))

    # --- Persist run metadata ---
    invoice_hash = result.invoice_summary.source_file_hash if result.invoice_summary else ""
    run_meta = ReconciliationRun(
        run_id=run_id,
        reporting_month=reporting_month,
        vendor=vendor,
        invoice_file_name=invoice_name,
        cash_report_file_name=cash_name,
        invoice_file_hash=invoice_hash,
        cash_report_file_hash="",
        uploaded_by=session.get("user", "web_user"),
        uploaded_date=datetime.utcnow(),
        status=result.status,
        invoice_stored_path=invoice_path,
        cash_report_stored_path=cash_path,
        exception_count=result.exception_count,
        blocking_exception_count=result.blocking_exception_count,
    )
    json_path = _save_result_json(result, run_id)
    save_run(run_meta, result_json_path=json_path)

    # Register file hashes for future duplicate detection
    if invoice_hash:
        register_file_hash(invoice_hash, run_id, "invoice")

    logger.info(
        "Upload processed: run_id=%s status=%s", run_id, result.status.value
    )

    # --- Redirect ---
    if result.has_blocking_exceptions:
        return redirect(url_for("reconciliation.exceptions", run_id=run_id))
    return redirect(url_for("reconciliation.review", run_id=run_id))


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@bp.route("/review/<run_id>", methods=["GET"])
def review(run_id: str):
    """Display the reconciliation review summary."""
    result_data = _load_result(run_id)
    run_meta = get_run(run_id)
    if not result_data or not run_meta:
        flash("Reconciliation run not found.", "danger")
        return redirect(url_for("reconciliation.upload"))

    return render_template(
        "review.html",
        run_id=run_id,
        result=result_data,
        run_meta=run_meta,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


@bp.route("/exceptions/<run_id>", methods=["GET"])
def exceptions(run_id: str):
    """Display the exceptions page for user review and resolution."""
    result_data = _load_result(run_id)
    run_meta = get_run(run_id)
    if not result_data or not run_meta:
        flash("Reconciliation run not found.", "danger")
        return redirect(url_for("reconciliation.upload"))

    # Skip the exceptions page if there is nothing for the user to resolve
    needs_review = any(
        exc.get("requires_user_review")
        for exc in result_data.get("property_exceptions", [])
    )
    if not needs_review:
        return redirect(url_for("reconciliation.review", run_id=run_id))

    # Load property master for dropdown choices
    from services.property_master_loader import load_property_master
    from services.rate_mapping import load_rate_rules

    try:
        properties = load_property_master(current_app.config["PROPERTY_MASTER_PATH"])
    except Exception:
        properties = []
    try:
        rate_rules = load_rate_rules(current_app.config["RATE_MAPPING_PATH"])
    except Exception:
        rate_rules = []

    return render_template(
        "exceptions.html",
        run_id=run_id,
        result=result_data,
        run_meta=run_meta,
        properties=properties,
        rate_rules=rate_rules,
    )


@bp.route("/exceptions/<run_id>/resolve", methods=["POST"])
def resolve_exceptions(run_id: str):
    """Process manual exception overrides and re-run reconciliation."""
    run_meta = get_run(run_id)
    if not run_meta:
        abort(404)

    form = request.form
    override_user = session.get("user", "web_user")

    # --- Parse property overrides from form ---
    property_overrides: dict = {}
    rate_overrides: dict = {}
    override_records: list = []

    for key, value in form.items():
        if key.startswith("property_override_") and value.strip():
            # key format: property_override_<url-encoded property name>
            prop_name = key.replace("property_override_", "", 1)
            property_overrides[prop_name] = value.strip()
            override_records.append(
                ManualOverride(
                    override_id=str(uuid.uuid4()),
                    run_id=run_id,
                    override_type="property_match",
                    invoice_property_name=prop_name,
                    resolved_property_id=value.strip(),
                    resolved_rate_rule_id=None,
                    excluded=False,
                    exclusion_reason=None,
                    override_user=override_user,
                    override_date=datetime.utcnow(),
                    notes=form.get(f"note_{prop_name}", ""),
                )
            )
        elif key.startswith("rate_override_") and value.strip():
            # key format: rate_override_<source_row>
            try:
                source_row = int(key.replace("rate_override_", "", 1))
                rate_overrides[source_row] = value.strip()
            except ValueError:
                pass

    # --- Persist overrides ---
    if override_records:
        save_overrides(override_records)

    # --- Re-run reconciliation with overrides ---
    try:
        result = run_reconciliation(
            invoice_file_path=run_meta["invoice_stored_path"],
            cash_report_file_path=run_meta["cash_report_stored_path"],
            reporting_month=run_meta["reporting_month"],
            vendor=run_meta["vendor"],
            property_master_path=current_app.config["PROPERTY_MASTER_PATH"],
            rate_mapping_path=current_app.config["RATE_MAPPING_PATH"],
            property_aliases_path=current_app.config["PROPERTY_ALIASES_PATH"],
            invoice_tolerance=current_app.config.get("INVOICE_TOLERANCE", Decimal("0.01")),
            balance_tolerance=current_app.config.get("BALANCE_TOLERANCE", Decimal("0.05")),
            manual_property_overrides=property_overrides,
            manual_rate_overrides=rate_overrides,
            uploaded_by=override_user,
            run_id=run_id,  # re-use same run_id
            generate_outputs=False,
            output_folder=current_app.config["OUTPUT_FOLDER"],
        )
    except Exception as exc:
        logger.error("Re-run error for %s: %s", run_id, exc, exc_info=True)
        flash(f"Re-run failed: {exc}", "danger")
        return redirect(url_for("reconciliation.exceptions", run_id=run_id))

    # Update stored result
    json_path = _save_result_json(result, run_id)
    updated_run = ReconciliationRun(
        run_id=run_id,
        reporting_month=run_meta["reporting_month"],
        vendor=run_meta["vendor"],
        invoice_file_name=run_meta["invoice_file_name"],
        cash_report_file_name=run_meta["cash_report_file_name"],
        invoice_file_hash=run_meta["invoice_file_hash"],
        cash_report_file_hash=run_meta["cash_report_file_hash"],
        uploaded_by=run_meta["uploaded_by"],
        uploaded_date=datetime.utcnow(),
        status=result.status,
        invoice_stored_path=run_meta["invoice_stored_path"],
        cash_report_stored_path=run_meta["cash_report_stored_path"],
        exception_count=result.exception_count,
        blocking_exception_count=result.blocking_exception_count,
    )
    save_run(updated_run, result_json_path=json_path)

    flash("Overrides applied and reconciliation re-run.", "success")

    if result.has_blocking_exceptions:
        return redirect(url_for("reconciliation.exceptions", run_id=run_id))
    return redirect(url_for("reconciliation.review", run_id=run_id))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@bp.route("/results/<run_id>", methods=["GET"])
def results(run_id: str):
    """Display final results page with download buttons."""
    result_data = _load_result(run_id)
    run_meta = get_run(run_id)
    if not result_data or not run_meta:
        flash("Reconciliation run not found.", "danger")
        return redirect(url_for("reconciliation.upload"))

    return render_template(
        "results.html",
        run_id=run_id,
        result=result_data,
        run_meta=run_meta,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@bp.route("/download/<run_id>/<file_type>", methods=["GET"])
def download(run_id: str, file_type: str):
    """Generate (if needed) and serve the requested output file.

    file_type: ``accounting``, ``audit``, or ``reconciliation_csv``
    """
    if file_type not in ("accounting", "audit", "reconciliation_csv"):
        abort(400)

    run_meta = get_run(run_id)
    if not run_meta:
        abort(404)

    # For reconciliation_csv, generate on demand from the stored JSON result
    if file_type == "reconciliation_csv":
        csv_path = run_meta.get("reconciliation_csv_path", "")
        if not csv_path or not Path(csv_path).exists():
            result_data = _load_result(run_id)
            if not result_data:
                abort(404)
            from services.reconciliation_service import run_reconciliation as _rerun
            try:
                result = _rerun(
                    invoice_file_path=run_meta["invoice_stored_path"],
                    cash_report_file_path=run_meta["cash_report_stored_path"],
                    reporting_month=run_meta["reporting_month"],
                    vendor=run_meta["vendor"],
                    property_master_path=current_app.config["PROPERTY_MASTER_PATH"],
                    rate_mapping_path=current_app.config["RATE_MAPPING_PATH"],
                    property_aliases_path=current_app.config["PROPERTY_ALIASES_PATH"],
                    run_id=run_id,
                    generate_outputs=False,
                    output_folder=current_app.config["OUTPUT_FOLDER"],
                )
                csv_path = generate_reconciliation_workbook(result, current_app.config["OUTPUT_FOLDER"])
                update_run_outputs(
                    run_id,
                    run_meta.get("accounting_output_path", ""),
                    run_meta.get("audit_output_path", ""),
                    result.status,
                    reconciliation_csv_path=csv_path,
                )
            except Exception as exc:
                logger.error("CSV generation failed: %s", exc, exc_info=True)
                flash("CSV generation failed.  Please contact support.", "danger")
                return redirect(url_for("reconciliation.results", run_id=run_id))

        if not csv_path or not Path(csv_path).exists():
            flash("Output file not found.", "danger")
            return redirect(url_for("reconciliation.results", run_id=run_id))

        return send_file(
            csv_path,
            as_attachment=True,
            download_name=f"{run_id}_reconciliation.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # Check if Excel outputs already exist
    accounting_path = run_meta.get("accounting_output_path", "")
    audit_path = run_meta.get("audit_output_path", "")

    if not accounting_path or not Path(accounting_path).exists() \
            or not audit_path or not Path(audit_path).exists():
        # Generate outputs now
        result_data = _load_result(run_id)
        if not result_data:
            abort(404)

        # Reconstruct a minimal result object for output generation
        from services.reconciliation_service import run_reconciliation as _rerun
        try:
            result = _rerun(
                invoice_file_path=run_meta["invoice_stored_path"],
                cash_report_file_path=run_meta["cash_report_stored_path"],
                reporting_month=run_meta["reporting_month"],
                vendor=run_meta["vendor"],
                property_master_path=current_app.config["PROPERTY_MASTER_PATH"],
                rate_mapping_path=current_app.config["RATE_MAPPING_PATH"],
                property_aliases_path=current_app.config["PROPERTY_ALIASES_PATH"],
                run_id=run_id,
                generate_outputs=True,
                output_folder=current_app.config["OUTPUT_FOLDER"],
            )
            accounting_path = result.accounting_output_path
            audit_path = result.audit_output_path
            update_run_outputs(
                run_id, accounting_path, audit_path, result.status,
                reconciliation_csv_path=result.reconciliation_csv_path,
            )
        except Exception as exc:
            logger.error("Output generation failed: %s", exc, exc_info=True)
            flash("Output generation failed.  Please contact support.", "danger")
            return redirect(url_for("reconciliation.results", run_id=run_id))

    target_path = accounting_path if file_type == "accounting" else audit_path

    if not target_path or not Path(target_path).exists():
        flash("Output file not found.", "danger")
        return redirect(url_for("reconciliation.results", run_id=run_id))

    download_name = (
        f"{run_id}_accounting_summary.xlsx"
        if file_type == "accounting"
        else f"{run_id}_detailed_audit.xlsx"
    )

    return send_file(
        target_path,
        as_attachment=True,
        download_name=download_name,
        mimetype=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
    )
