"""Tests for the property matching engine."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models.reconciliation_models import ExceptionType, MatchStatus
from services.property_matching import match_invoice_properties
from tests.conftest import make_invoice_line


class TestVendorPropertyIDMatch:
    """Test 1 – Match by Vendor Property ID."""

    def test_matches_by_vendor_property_id(self, property_master, property_aliases):
        line = make_invoice_line(
            description="Some description",
            vendor_property_id="RP-1001",
        )
        lines, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
        )
        assert lines[0].internal_property_id == "CB-0001"
        assert lines[0].match_status == MatchStatus.ID_MATCH
        assert not any(
            e.exception_type == ExceptionType.UNMATCHED_PROPERTY for e in exceptions
        )


class TestPMSPropertyIDMatch:
    """Test 2 – Match by PMS Property ID."""

    def test_matches_by_pms_id(self, property_master, property_aliases):
        line = make_invoice_line(
            description="River View Heights",
            vendor_property_id="PMS-104",  # This is the PMS ID
        )
        lines, _ = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
        )
        assert lines[0].internal_property_id == "CB-0004"


class TestApprovedAliasMatch:
    """Test 3 – Match via approved alias."""

    def test_approved_alias_matches(self, property_master, property_aliases):
        line = make_invoice_line(description="555 Blvd")  # known alias
        lines, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
        )
        assert lines[0].internal_property_id == "CB-0002"
        assert lines[0].match_status == MatchStatus.ALIAS_MATCH

    def test_unapproved_alias_does_not_match(self, property_master):
        from models.reconciliation_models import PropertyAlias
        from datetime import date

        unapproved_alias = [
            PropertyAlias(
                alias="555 blvd",
                internal_property_id="CB-0002",
                approved=False,
                created_date=date(2024, 6, 1),
            )
        ]
        line = make_invoice_line(description="555 Blvd")
        lines, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=unapproved_alias,  # unapproved only
            vendor="Rent Plus",
            reporting_month="2026-07",
            fuzzy_threshold=99,  # disable fuzzy
        )
        # Should NOT match via unapproved alias
        assert lines[0].match_status != MatchStatus.ALIAS_MATCH


class TestFuzzyMatchRemainsSuggestion:
    """Test 4 – Fuzzy match is a suggestion only, never auto-approved."""

    def test_fuzzy_match_is_not_approved(self, property_master, property_aliases):
        line = make_invoice_line(
            description="Sunrise Apartmentsss",  # typo
            vendor_property_id="",
        )
        lines, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
            fuzzy_threshold=70,
        )
        assert lines[0].match_status == MatchStatus.FUZZY_SUGGESTION
        assert lines[0].internal_property_id is None  # not set
        fuzzy_excs = [
            e for e in exceptions
            if e.exception_type == ExceptionType.FUZZY_MATCH_SUGGESTION
        ]
        assert fuzzy_excs
        assert all(e.requires_user_review for e in fuzzy_excs)


class TestMissingExpectedProperty:
    """Test 5 – Active property not on invoice raises WARNING."""

    def test_missing_property_exception(self, property_master, property_aliases):
        # Only provide lines for 4 out of 5 active properties
        lines = [
            make_invoice_line(description="Sunrise Apartments", vendor_property_id="RP-1001"),
            make_invoice_line(description="555 Boulevard", vendor_property_id="RP-1002"),
            make_invoice_line(description="Oak Tree Commons", vendor_property_id="RP-1003"),
            make_invoice_line(description="River View Heights", vendor_property_id="RP-1004"),
            # CB-0005 (The Grand Reserve) is MISSING
        ]
        _, exceptions = match_invoice_properties(
            invoice_lines=lines,
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
        )
        missing = [
            e for e in exceptions
            if e.exception_type == ExceptionType.MISSING_EXPECTED_PROPERTY
        ]
        assert any(e.suggested_property_id == "CB-0005" for e in missing)


class TestUnexpectedProperty:
    """Test 6 – Invoice property that matches an inactive property raises WARNING."""

    def test_inactive_property_on_invoice(self, property_aliases):
        from datetime import date
        from models.reconciliation_models import PropertyMaster

        pm = [
            PropertyMaster(
                internal_property_id="CB-OLD",
                pms_property_id="PMS-999",
                vendor_property_id="RP-9999",
                property_name="Old Property",
                normalized_property_name="old property",
                vendor="Rent Plus",
                program_status="terminated",
                program_start_date=date(2020, 1, 1),
                program_end_date=date(2022, 12, 31),
                charge_code="RENTPLUS",
                active=False,
            )
        ]
        line = make_invoice_line(
            description="Old Property",
            vendor_property_id="RP-9999",
        )
        _, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=pm,
            property_aliases=[],
            vendor="Rent Plus",
            reporting_month="2026-07",
        )
        unexpected = [
            e for e in exceptions
            if e.exception_type == ExceptionType.UNEXPECTED_PROPERTY
        ]
        assert unexpected


class TestManualOverride:
    """Test that manual overrides are applied."""

    def test_override_resolves_unmatched(self, property_master, property_aliases):
        line = make_invoice_line(description="Unknown Name XYZ")
        overrides = {"unknown name xyz": "CB-0001"}
        lines, exceptions = match_invoice_properties(
            invoice_lines=[line],
            property_master=property_master,
            property_aliases=property_aliases,
            vendor="Rent Plus",
            reporting_month="2026-07",
            manual_overrides=overrides,
        )
        assert lines[0].internal_property_id == "CB-0001"
        assert lines[0].manual_override is True
