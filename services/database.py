"""
SQLite persistence layer.

Stores reconciliation run metadata and manual overrides.  SQLite is used for
Phase 1 because the schema maps cleanly to Azure SQL for a future migration.

Usage::

    from services.database import init_db, save_run, get_run, list_runs

    init_db()
    save_run(run_metadata)
    run = get_run("REC-202607-RENTPLUS-0001")
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional

from models.reconciliation_models import ManualOverride, ReconciliationRun, ReconciliationStatus

logger = logging.getLogger(__name__)

_DB_PATH: Optional[str] = None


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_db(db_path: Optional[str] = None) -> None:
    """Create tables if they do not exist.

    Args:
        db_path: Absolute path to the SQLite file.  If ``None``, the path is
                 read from the ``DATABASE_PATH`` config value.
    """
    global _DB_PATH
    if db_path:
        _DB_PATH = db_path
    else:
        try:
            from config import Config
            _DB_PATH = Config.DATABASE_PATH
        except ImportError:
            _DB_PATH = "reconciliation.db"

    # Ensure the parent directory exists (e.g. a persistent Azure path not yet created)
    parent = Path(_DB_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    with _connect() as conn:
        conn.executescript(_DDL)
        # Migrations: add columns that predate the current schema
        for migration_sql in [
            "ALTER TABLE reconciliation_runs ADD COLUMN reconciliation_csv_path TEXT",
            "ALTER TABLE reconciliation_runs ADD COLUMN run_type TEXT DEFAULT 'invoice_cash'",
        ]:
            try:
                conn.execute(migration_sql)
            except Exception:
                pass  # Column already exists
    logger.info("Database initialised at %s", _DB_PATH)


_DDL = """
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id                   TEXT PRIMARY KEY,
    reporting_month          TEXT NOT NULL,
    vendor                   TEXT NOT NULL,
    invoice_file_name        TEXT,
    cash_report_file_name    TEXT,
    invoice_file_hash        TEXT,
    cash_report_file_hash    TEXT,
    uploaded_by              TEXT,
    uploaded_date            TEXT,
    status                   TEXT,
    invoice_stored_path      TEXT,
    cash_report_stored_path  TEXT,
    accounting_output_path   TEXT,
    audit_output_path        TEXT,
    reconciliation_csv_path  TEXT,
    exception_count          INTEGER DEFAULT 0,
    blocking_exception_count INTEGER DEFAULT 0,
    notes                    TEXT,
    result_json_path         TEXT,
    run_type                 TEXT DEFAULT 'invoice_cash'
);

CREATE TABLE IF NOT EXISTS manual_overrides (
    override_id          TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    override_type        TEXT NOT NULL,
    invoice_property_name TEXT,
    resolved_property_id TEXT,
    resolved_rate_rule_id TEXT,
    excluded             INTEGER DEFAULT 0,
    exclusion_reason     TEXT,
    override_user        TEXT,
    override_date        TEXT,
    notes                TEXT,
    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(run_id)
);

CREATE TABLE IF NOT EXISTS known_file_hashes (
    hash_value     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    file_type      TEXT,
    recorded_date  TEXT
);
"""


# ---------------------------------------------------------------------------
# Run CRUD
# ---------------------------------------------------------------------------


def save_run(run: ReconciliationRun, result_json_path: str = "") -> None:
    """Insert or replace a reconciliation run record."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reconciliation_runs (
                run_id, reporting_month, vendor,
                invoice_file_name, cash_report_file_name,
                invoice_file_hash, cash_report_file_hash,
                uploaded_by, uploaded_date, status,
                invoice_stored_path, cash_report_stored_path,
                accounting_output_path, audit_output_path,
                reconciliation_csv_path,
                exception_count, blocking_exception_count,
                notes, result_json_path, run_type
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run.run_id,
                run.reporting_month,
                run.vendor,
                run.invoice_file_name,
                run.cash_report_file_name,
                run.invoice_file_hash,
                run.cash_report_file_hash,
                run.uploaded_by,
                run.uploaded_date.isoformat() if run.uploaded_date else None,
                run.status.value,
                run.invoice_stored_path,
                run.cash_report_stored_path,
                run.accounting_output_path,
                run.audit_output_path,
                run.reconciliation_csv_path if hasattr(run, 'reconciliation_csv_path') else "",
                run.exception_count,
                run.blocking_exception_count,
                run.notes,
                result_json_path,
                getattr(run, 'run_type', 'invoice_cash'),
            ),
        )
    logger.info("Saved run: %s  status=%s", run.run_id, run.status.value)


def get_run(run_id: str) -> Optional[dict]:
    """Return a run record as a dict, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_runs(limit: int = 50) -> List[dict]:
    """Return the most recent reconciliation runs."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY uploaded_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_run_outputs(
    run_id: str,
    accounting_path: str,
    audit_path: str,
    status: ReconciliationStatus,
    reconciliation_csv_path: str = "",
) -> None:
    """Update the output paths and status for an existing run."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE reconciliation_runs
            SET accounting_output_path  = ?,
                audit_output_path       = ?,
                reconciliation_csv_path = ?,
                status                  = ?
            WHERE run_id = ?
            """,
            (accounting_path, audit_path, reconciliation_csv_path, status.value, run_id),
        )


def get_next_sequence(reporting_month: str, vendor: str) -> int:
    """Return the next available run sequence number for the given month/vendor."""
    vendor_slug = vendor.upper().replace(" ", "").replace("-", "")[:12]
    month_slug = reporting_month.replace("-", "")
    prefix = f"REC-{month_slug}-{vendor_slug}-"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_runs WHERE run_id LIKE ?",
            (f"{prefix}%",),
        ).fetchone()
    return (row[0] or 0) + 1


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


def save_overrides(overrides: List[ManualOverride]) -> None:
    """Persist a list of manual overrides."""
    with _connect() as conn:
        for o in overrides:
            conn.execute(
                """
                INSERT OR REPLACE INTO manual_overrides (
                    override_id, run_id, override_type,
                    invoice_property_name, resolved_property_id,
                    resolved_rate_rule_id, excluded, exclusion_reason,
                    override_user, override_date, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    o.override_id,
                    o.run_id,
                    o.override_type,
                    o.invoice_property_name,
                    o.resolved_property_id,
                    o.resolved_rate_rule_id,
                    1 if o.excluded else 0,
                    o.exclusion_reason,
                    o.override_user,
                    o.override_date.isoformat() if o.override_date else None,
                    o.notes,
                ),
            )


def get_overrides(run_id: str) -> List[dict]:
    """Return all manual overrides for a given run."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_overrides WHERE run_id = ?", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# File hash registry
# ---------------------------------------------------------------------------


def register_file_hash(hash_value: str, run_id: str, file_type: str) -> None:
    """Record a processed file hash to support duplicate detection."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO known_file_hashes
            (hash_value, run_id, file_type, recorded_date)
            VALUES (?,?,?,?)
            """,
            (hash_value, run_id, file_type, datetime.utcnow().isoformat()),
        )


def get_known_hashes() -> List[str]:
    """Return all previously seen file hashes."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT hash_value FROM known_file_hashes"
        ).fetchall()
    return [r[0] for r in rows]


def delete_run(run_id: str) -> dict:
    """Delete a reconciliation run and all associated data.

    Removes:
    - The ``reconciliation_runs`` row
    - All ``manual_overrides`` rows for the run
    - The ``known_file_hashes`` rows recorded for this run
    - The uploaded invoice/cash report files (if they still exist on disk)
    - The accounting output, audit output, and result JSON files (if present)

    Returns a dict summarising what was deleted, keyed by category.
    """
    deleted: dict = {
        "run_id": run_id,
        "db_rows_removed": 0,
        "files_removed": [],
        "files_missing": [],
    }

    with _connect() as conn:
        run = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

        if run is None:
            return deleted  # Nothing to do

        run = dict(run)

        # Collect file paths stored in the DB row
        file_columns = [
            "invoice_stored_path",
            "cash_report_stored_path",
            "accounting_output_path",
            "audit_output_path",
            "result_json_path",
        ]
        paths_to_remove = [run[col] for col in file_columns if run.get(col)]

        # Delete child rows first
        conn.execute("DELETE FROM manual_overrides WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM known_file_hashes WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM reconciliation_runs WHERE run_id = ?", (run_id,))
        deleted["db_rows_removed"] = 3  # upper bound; actual may vary

    # Delete files outside the transaction so DB is already committed
    import os as _os
    for path in paths_to_remove:
        try:
            if _os.path.exists(path):
                _os.remove(path)
                deleted["files_removed"].append(path)
            else:
                deleted["files_missing"].append(path)
        except OSError as exc:
            logger.warning("Could not delete file %s: %s", path, exc)
            deleted["files_missing"].append(path)

    logger.info(
        "Deleted run %s.  Files removed: %d.  Files missing: %d.",
        run_id,
        len(deleted["files_removed"]),
        len(deleted["files_missing"]),
    )
    return deleted


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    """Yield a database connection with row_factory set."""
    if _DB_PATH is None:
        raise RuntimeError(
            "Database not initialised.  Call init_db() first."
        )
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
