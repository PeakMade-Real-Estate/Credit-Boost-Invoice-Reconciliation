"""
Property master loader.

Reads the CSV (or JSON) property master file and returns typed
``PropertyMaster`` objects.  This module is intentionally separate from the
matching engine so the loader can later be swapped for a SharePoint or
Azure SQL source without changing downstream logic.

Usage::

    from services.property_master_loader import load_property_master

    properties = load_property_master("data/property_master.csv")
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from models.reconciliation_models import PropertyAlias, PropertyMaster
from services.utils import normalize_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Property master
# ---------------------------------------------------------------------------


def load_property_master(file_path: str) -> List[PropertyMaster]:
    """Load the property master list from CSV.

    Args:
        file_path: Absolute path to ``property_master.csv``.

    Returns:
        List of ``PropertyMaster`` dataclass instances.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If required columns are missing.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Property master file not found: {file_path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    required = {
        "internal_property_id",
        "pms_property_id",
        "vendor_property_id",
        "property_name",
        "vendor",
        "active",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Property master is missing required columns: {sorted(missing)}"
        )

    records: List[PropertyMaster] = []
    for _, row in df.iterrows():
        active_raw = str(row.get("active", "false")).strip().lower()
        active = active_raw in ("true", "1", "yes", "active")

        records.append(
            PropertyMaster(
                internal_property_id=str(row["internal_property_id"]).strip(),
                pms_property_id=str(row.get("pms_property_id", "")).strip(),
                vendor_property_id=str(row.get("vendor_property_id", "")).strip(),
                property_name=str(row["property_name"]).strip(),
                normalized_property_name=normalize_text(
                    str(row.get("normalized_property_name") or row["property_name"])
                ),
                vendor=str(row["vendor"]).strip(),
                program_status=str(row.get("program_status", "")).strip(),
                program_start_date=_parse_date(row.get("program_start_date")),
                program_end_date=_parse_date(row.get("program_end_date")),
                charge_code=str(row.get("charge_code", "")).strip(),
                active=active,
            )
        )

    logger.info("Loaded %d property master records from %s", len(records), path.name)
    return records


# ---------------------------------------------------------------------------
# Property aliases
# ---------------------------------------------------------------------------


def load_property_aliases(file_path: str) -> List[PropertyAlias]:
    """Load the property alias table from CSV.

    Args:
        file_path: Absolute path to ``property_aliases.csv``.

    Returns:
        List of ``PropertyAlias`` instances (approved entries only).
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning("Property aliases file not found: %s; using empty list.", file_path)
        return []

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    records: List[PropertyAlias] = []
    for _, row in df.iterrows():
        approved_raw = str(row.get("approved", "false")).strip().lower()
        approved = approved_raw in ("true", "1", "yes")
        records.append(
            PropertyAlias(
                alias=normalize_text(str(row.get("alias", "")).strip()),
                internal_property_id=str(row.get("internal_property_id", "")).strip(),
                approved=approved,
                created_date=_parse_date(row.get("created_date")),
            )
        )

    approved_count = sum(1 for r in records if r.approved)
    logger.info(
        "Loaded %d aliases (%d approved) from %s",
        len(records),
        approved_count,
        path.name,
    )
    return [r for r in records if r.approved]


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def build_property_index(
    properties: List[PropertyMaster],
) -> Dict[str, PropertyMaster]:
    """Return a dict keyed by ``vendor_property_id`` (non-empty values only)."""
    return {
        p.vendor_property_id: p
        for p in properties
        if p.vendor_property_id
    }


def build_pms_index(
    properties: List[PropertyMaster],
) -> Dict[str, PropertyMaster]:
    """Return a dict keyed by ``pms_property_id``."""
    return {p.pms_property_id: p for p in properties if p.pms_property_id}


def build_internal_index(
    properties: List[PropertyMaster],
) -> Dict[str, PropertyMaster]:
    """Return a dict keyed by ``internal_property_id``."""
    return {p.internal_property_id: p for p in properties if p.internal_property_id}


def build_alias_index(
    aliases: List[PropertyAlias],
) -> Dict[str, str]:
    """Return a dict mapping normalised alias → internal_property_id."""
    return {a.alias: a.internal_property_id for a in aliases if a.approved}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: object) -> Optional[date]:
    """Try to parse a date string; return None on failure."""
    if not value or str(value).strip() in ("", "nan", "None"):
        return None
    import datetime as dt

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None
