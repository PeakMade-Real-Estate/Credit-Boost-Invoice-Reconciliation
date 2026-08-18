"""Shared test fixtures and helpers."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from models.reconciliation_models import (
    InvoiceLine,
    MatchStatus,
    PropertyAlias,
    PropertyMaster,
    RateRule,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Property master fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def property_master() -> list:
    """Five active Rent Plus properties."""
    return [
        PropertyMaster(
            internal_property_id="CB-0001",
            pms_property_id="PMS-101",
            vendor_property_id="RP-1001",
            property_name="Sunrise Apartments",
            normalized_property_name="sunrise apartments",
            vendor="Rent Plus",
            program_status="active",
            program_start_date=date(2024, 1, 1),
            program_end_date=None,
            charge_code="RENTPLUS",
            active=True,
        ),
        PropertyMaster(
            internal_property_id="CB-0002",
            pms_property_id="PMS-102",
            vendor_property_id="RP-1002",
            property_name="555 Boulevard",
            normalized_property_name="555 boulevard",
            vendor="Rent Plus",
            program_status="active",
            program_start_date=date(2024, 3, 1),
            program_end_date=None,
            charge_code="RENTPLUS",
            active=True,
        ),
        PropertyMaster(
            internal_property_id="CB-0003",
            pms_property_id="PMS-103",
            vendor_property_id="RP-1003",
            property_name="Oak Tree Commons",
            normalized_property_name="oak tree commons",
            vendor="Rent Plus",
            program_status="active",
            program_start_date=date(2023, 6, 1),
            program_end_date=None,
            charge_code="RENTPLUS",
            active=True,
        ),
        PropertyMaster(
            internal_property_id="CB-0004",
            pms_property_id="PMS-104",
            vendor_property_id="RP-1004",
            property_name="River View Heights",
            normalized_property_name="river view heights",
            vendor="Rent Plus",
            program_status="active",
            program_start_date=date(2024, 1, 1),
            program_end_date=None,
            charge_code="RENTPLUS",
            active=True,
        ),
        PropertyMaster(
            internal_property_id="CB-0005",
            pms_property_id="PMS-105",
            vendor_property_id="RP-1005",
            property_name="The Grand Reserve",
            normalized_property_name="the grand reserve",
            vendor="Rent Plus",
            program_status="active",
            program_start_date=date(2024, 9, 1),
            program_end_date=None,
            charge_code="RENTPLUS",
            active=True,
        ),
    ]


@pytest.fixture
def property_aliases() -> list:
    return [
        PropertyAlias(
            alias="555 blvd",
            internal_property_id="CB-0002",
            approved=True,
            created_date=date(2024, 6, 1),
        ),
        PropertyAlias(
            alias="sunrise apts",
            internal_property_id="CB-0001",
            approved=True,
            created_date=date(2024, 6, 1),
        ),
    ]


# ---------------------------------------------------------------------------
# Rate rule fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rate_rules() -> list:
    return [
        RateRule(
            rate_rule_id="RR-001",
            vendor="Rent Plus",
            property_id=None,
            invoice_unit_price=Decimal("7.85"),
            rate_name="Standard Single Policy",
            policy_multiplier=Decimal("1"),
            sign=Decimal("1"),
            vendor_cost_per_policy=Decimal("7.85"),
            peak_share_per_policy=Decimal("2.50"),
            owner_share_per_policy=Decimal("5.35"),
            resident_charge_per_policy=Decimal("9.99"),
            effective_start_date=date(2024, 1, 1),
            effective_end_date=None,
            active=True,
        ),
        RateRule(
            rate_rule_id="RR-002",
            vendor="Rent Plus",
            property_id=None,
            invoice_unit_price=Decimal("15.70"),
            rate_name="Standard Double Policy",
            policy_multiplier=Decimal("2"),
            sign=Decimal("1"),
            vendor_cost_per_policy=Decimal("7.85"),
            peak_share_per_policy=Decimal("2.50"),
            owner_share_per_policy=Decimal("5.35"),
            resident_charge_per_policy=Decimal("9.99"),
            effective_start_date=date(2024, 1, 1),
            effective_end_date=None,
            active=True,
        ),
        RateRule(
            rate_rule_id="RR-003",
            vendor="Rent Plus",
            property_id=None,
            invoice_unit_price=Decimal("23.55"),
            rate_name="Standard Triple Policy",
            policy_multiplier=Decimal("3"),
            sign=Decimal("1"),
            vendor_cost_per_policy=Decimal("7.85"),
            peak_share_per_policy=Decimal("2.50"),
            owner_share_per_policy=Decimal("5.35"),
            resident_charge_per_policy=Decimal("9.99"),
            effective_start_date=date(2024, 1, 1),
            effective_end_date=None,
            active=True,
        ),
        RateRule(
            rate_rule_id="RR-004",
            vendor="Rent Plus",
            property_id=None,
            invoice_unit_price=Decimal("31.40"),
            rate_name="Standard Quad Policy",
            policy_multiplier=Decimal("4"),
            sign=Decimal("1"),
            vendor_cost_per_policy=Decimal("7.85"),
            peak_share_per_policy=Decimal("2.50"),
            owner_share_per_policy=Decimal("5.35"),
            resident_charge_per_policy=Decimal("9.99"),
            effective_start_date=date(2024, 1, 1),
            effective_end_date=None,
            active=True,
        ),
        RateRule(
            rate_rule_id="RR-005",
            vendor="Rent Plus",
            property_id=None,
            invoice_unit_price=Decimal("-7.85"),
            rate_name="Reversal Single Policy",
            policy_multiplier=Decimal("1"),
            sign=Decimal("-1"),
            vendor_cost_per_policy=Decimal("7.85"),
            peak_share_per_policy=Decimal("2.50"),
            owner_share_per_policy=Decimal("5.35"),
            resident_charge_per_policy=Decimal("9.99"),
            effective_start_date=date(2024, 1, 1),
            effective_end_date=None,
            active=True,
        ),
        RateRule(
            rate_rule_id="RR-007",
            vendor="Rent Plus",
            property_id="CB-0003",
            invoice_unit_price=Decimal("6.50"),
            rate_name="Legacy Single Policy",
            policy_multiplier=Decimal("1"),
            sign=Decimal("1"),
            vendor_cost_per_policy=Decimal("6.50"),
            peak_share_per_policy=Decimal("2.00"),
            owner_share_per_policy=Decimal("4.50"),
            resident_charge_per_policy=Decimal("8.99"),
            effective_start_date=date(2022, 1, 1),
            effective_end_date=date(2026, 12, 31),
            active=True,
        ),
    ]


# ---------------------------------------------------------------------------
# Invoice line helper
# ---------------------------------------------------------------------------


def make_invoice_line(
    description: str = "Test Property",
    vendor_property_id: str = "",
    quantity: str = "1",
    unit_price: str = "7.85",
    amount: str = "7.85",
    source_row: int = 2,
) -> InvoiceLine:
    return InvoiceLine(
        invoice_id="INV-TEST",
        reporting_month="2026-07",
        vendor="Rent Plus",
        source_row=source_row,
        original_description=description,
        normalized_property_name=description.lower().strip(),
        vendor_property_id=vendor_property_id,
        original_quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        line_amount=Decimal(amount),
        match_status=MatchStatus.UNMATCHED,
    )
