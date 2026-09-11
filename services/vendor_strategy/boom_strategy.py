"""
Credit Boost powered by Boom reconciliation strategy.

Implements ``VendorReconciliationStrategy`` for Boom transaction exports.

Key differences from RentPlus:
- Accepts CSV or XLSX (no PDF support).
- Qualifying rows are identified by configurable Category, Transaction Type,
  Template Name, and Subject Type filter values.
- QTY = count of **distinct qualifying transaction IDs** per property.
- AMOUNT = sum of normalised transaction amounts per property.
- Property matching uses an approved Boom → internal property roll-up mapping
  (``boom_property_rollups.csv``), not the RentPlus property alias table.
- No rate multipliers are used; rate exceptions are never raised.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models.boom_models import BoomPropertyRollup, BoomTransactionLine
from models.reconciliation_models import (
    ExceptionSeverity,
    ExceptionType,
    InvoiceSummary,
    MatchStatus,
    PropertyException,
    PropertyMaster,
    PropertyResult,
    RateException,
    ValidationResult,
)
from services.boom_invoice_parser import parse_boom_file
from services.utils import normalize_text
from services.vendor_strategy.base import VendorReconciliationStrategy

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "vendors" / "credit_boost_boom.yaml"


class BoomReconciliationStrategy(VendorReconciliationStrategy):
    """Reconciliation strategy for Credit Boost powered by Boom exports."""

    def __init__(self) -> None:
        import yaml

        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            self._config: dict = yaml.safe_load(fh)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def vendor_code(self) -> str:
        return "credit_boost_boom"

    @property
    def display_name(self) -> str:
        return "Credit Boost powered by Boom"

    @property
    def accepted_file_types(self) -> frozenset:
        return frozenset(self._config.get("accepted_file_types", ["csv", "xlsx"]))

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------

    def load_vendor_reference_data(
        self,
        *,
        property_rollups_path: Optional[str] = None,
        **_kwargs,
    ) -> dict:
        """Load the approved Boom property roll-up mapping."""
        rollups_path = property_rollups_path or str(
            _PROJECT_ROOT / self._config["property_rollups_path"]
        )
        rollups = _load_boom_rollups(rollups_path)
        # Index by normalised boom_property_id for O(1) lookup
        return {
            "rollups": {
                normalize_text(r.boom_property_id): r
                for r in rollups
                if r.approved
            }
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_vendor_file(
        self,
        file_path: str,
        reporting_month: str,
    ) -> Tuple[List[BoomTransactionLine], InvoiceSummary]:
        return parse_boom_file(file_path, reporting_month)

    # ------------------------------------------------------------------
    # Normalisation / qualifying filter
    # ------------------------------------------------------------------

    def normalize_vendor_data(
        self, raw_lines: list
    ) -> List[BoomTransactionLine]:
        """Set ``is_qualifying`` on each line based on configured filter values.

        All non-empty filter dimensions must match (AND logic).  An empty
        filter list means that dimension is not checked.
        """
        filters = self._config.get("qualifying_filters", {})
        q_categories = {v.lower() for v in filters.get("category", [])}
        q_types = {v.lower() for v in filters.get("transaction_type", [])}
        q_templates = {v.lower() for v in filters.get("template_name", [])}
        q_subjects = {v.lower() for v in filters.get("subject_type", [])}

        for line in raw_lines:
            qualifies = True
            if q_categories and line.category.lower() not in q_categories:
                qualifies = False
            if q_types and line.transaction_type.lower() not in q_types:
                qualifies = False
            if q_templates and line.template_name.lower() not in q_templates:
                qualifies = False
            if q_subjects and line.subject_type.lower() not in q_subjects:
                qualifies = False
            line.is_qualifying = qualifies

        qualifying_count = sum(1 for l in raw_lines if l.is_qualifying)
        logger.info(
            "Boom normalisation: %d/%d lines qualifying.",
            qualifying_count,
            len(raw_lines),
        )
        return raw_lines

    # ------------------------------------------------------------------
    # Vendor-specific validation
    # ------------------------------------------------------------------

    def validate_vendor_data(
        self,
        lines: list,
        invoice_summary: InvoiceSummary,
    ) -> List[ValidationResult]:
        """Detect duplicate qualifying transaction IDs (BLOCKING)."""
        qualifying = [l for l in lines if getattr(l, "is_qualifying", False)]
        seen: Dict[str, int] = {}
        duplicates = []
        for line in qualifying:
            tid = line.transaction_id
            if tid in seen:
                duplicates.append(tid)
            else:
                seen[tid] = 1

        if duplicates:
            deduped = sorted(set(duplicates))
            return [
                ValidationResult(
                    check_name="boom_duplicate_transaction_ids",
                    passed=False,
                    severity=ExceptionSeverity.BLOCKING,
                    actual_value=deduped,
                    message=(
                        f"Duplicate qualifying transaction IDs detected: {deduped}. "
                        "Each transaction ID must appear at most once in the "
                        "qualifying rows."
                    ),
                )
            ]

        return [
            ValidationResult(
                check_name="boom_duplicate_transaction_ids",
                passed=True,
                severity=ExceptionSeverity.INFO,
                message="No duplicate qualifying transaction IDs.",
            )
        ]

    # ------------------------------------------------------------------
    # Property matching
    # ------------------------------------------------------------------

    def match_to_properties(
        self,
        lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        fuzzy_threshold: int = 80,
        manual_overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[BoomTransactionLine], List[PropertyException]]:
        """Match Boom lines to properties using the approved rollup mapping.

        The ``boom_property_id`` on each transaction line is normalised and
        looked up in the rollup index.  Unmatched lines generate an
        UNMATCHED_PROPERTY exception.  Fuzzy matching is not performed because
        the rollup is an explicit, approved mapping.
        """
        rollups: Dict[str, BoomPropertyRollup] = vendor_reference_data["rollups"]
        pm_by_internal: Dict[str, PropertyMaster] = {
            p.internal_property_id: p for p in property_master
        }
        overrides: Dict[str, str] = {
            normalize_text(k): v for k, v in (manual_overrides or {}).items()
        }

        exceptions: List[PropertyException] = []

        for line in lines:
            norm_prop = normalize_text(line.boom_property_id)

            # Manual override wins
            if norm_prop in overrides:
                internal_id = overrides[norm_prop]
                pm = pm_by_internal.get(internal_id)
                line.internal_property_id = internal_id
                line.pms_property_id = pm.pms_property_id if pm else ""
                line.match_status = MatchStatus.ID_MATCH
                continue

            rollup = rollups.get(norm_prop)
            if rollup:
                pm = pm_by_internal.get(rollup.internal_property_id)
                line.internal_property_id = rollup.internal_property_id
                line.pms_property_id = rollup.pms_property_id
                line.match_status = MatchStatus.ALIAS_MATCH
            else:
                line.match_status = MatchStatus.UNMATCHED
                line.exception_reason = (
                    f"No approved rollup found for Boom property "
                    f"{line.boom_property_id!r}."
                )
                exceptions.append(
                    PropertyException(
                        exception_type=ExceptionType.UNMATCHED_PROPERTY,
                        severity=ExceptionSeverity.BLOCKING,
                        invoice_property_name=line.boom_property_id,
                        requires_user_review=True,
                        message=line.exception_reason,
                    )
                )

        return lines, exceptions

    # ------------------------------------------------------------------
    # Aggregation (no rate mapping)
    # ------------------------------------------------------------------

    def aggregate_and_rate_map(
        self,
        matched_lines: list,
        property_master: List[PropertyMaster],
        vendor_reference_data: dict,
        reporting_month: str,
        *,
        manual_rate_overrides: Optional[Dict[int, str]] = None,
    ) -> Tuple[List[PropertyResult], List[RateException]]:
        """Aggregate qualifying Boom lines.

        QTY  = count of distinct qualifying transaction IDs per property.
        AMOUNT = sum of transaction amounts for qualifying lines.

        No rate multipliers are applied.  No rate exceptions are produced.
        """
        pm_by_internal: Dict[str, PropertyMaster] = {
            p.internal_property_id: p for p in property_master
        }

        try:
            from config import Config

            flat_rate = Config.CREDIT_BOOST_BASE_PRICE
        except Exception:
            flat_rate = Decimal(str(self._config.get("flat_rate_per_resident", "6.50")))

        groups: Dict[str, List[BoomTransactionLine]] = {}
        for line in matched_lines:
            if not line.is_qualifying:
                continue
            pid = line.internal_property_id or f"__unmatched__{normalize_text(line.boom_property_id)}"
            groups.setdefault(pid, []).append(line)

        results: List[PropertyResult] = []
        for internal_id, lines in groups.items():
            pm = pm_by_internal.get(internal_id)
            first = lines[0]

            unique_tx_ids = {l.transaction_id for l in lines}
            qty = Decimal(str(len(unique_tx_ids)))
            amount = qty * flat_rate

            pr = PropertyResult(
                internal_property_id=internal_id,
                pms_property_id=first.pms_property_id or (pm.pms_property_id if pm else ""),
                vendor_property_id="",
                property_name=pm.property_name if pm else first.boom_property_id,
                reporting_month=reporting_month,
                charge_code=pm.charge_code if pm else "",
                vendor=self.display_name,
                net_policy_quantity=qty,
                standard_policy_quantity=qty,
                invoice_amount_owed=amount,
                positive_invoice_amount=max(amount, Decimal("0")),
                transition_date=pm.program_start_date if pm else None,
            )
            results.append(pr)

        return results, []  # Boom never produces rate exceptions

    # ------------------------------------------------------------------
    # Financial calculations
    # ------------------------------------------------------------------

    def calculate_property_financials(
        self,
        property_results: List[PropertyResult],
        matched_lines: list,
        vendor_reference_data: dict,
    ) -> List[PropertyResult]:
        """Calculate actual revenue share without rate rule multipliers."""
        for pr in property_results:
            pr.amount_to_pull = pr.invoice_amount_owed
            pr.actual_property_revenue_share = pr.cash_received - pr.invoice_amount_owed

            if pr.cash_received < pr.invoice_amount_owed:
                pr.validation_status = "warning"
            else:
                pr.validation_status = "ok"

        return property_results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_boom_rollups(file_path: str) -> List[BoomPropertyRollup]:
    """Load and return all rows from the Boom property roll-up CSV."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Boom rollup file not found: {file_path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    rollups: List[BoomPropertyRollup] = []
    for _, row in df.iterrows():
        approved_raw = str(row.get("approved", "false")).strip().lower()
        rollups.append(
            BoomPropertyRollup(
                boom_property_id=str(row.get("boom_property_id", "")).strip(),
                internal_property_id=str(row.get("internal_property_id", "")).strip(),
                pms_property_id=str(row.get("pms_property_id", "")).strip(),
                boom_property_name=str(row.get("boom_property_name", "")).strip(),
                approved=approved_raw in ("true", "1", "yes"),
            )
        )
    return rollups
