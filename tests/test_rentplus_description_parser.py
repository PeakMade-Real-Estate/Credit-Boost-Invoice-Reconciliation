"""
Unit tests for the RentPlus description-parsing layer.

All tests in this file use synthetic strings only (no PDF file required).
The optional integration test is skipped automatically when the real invoice
(Test Files/May 2026.pdf) is absent.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from services.pdf_invoice_parser import (
    extract_rentplus_property_name,
    normalize_rentplus_description,
    normalize_property_name_for_matching,
    RentPlusPDFParser,
)

REAL_PDF = Path(__file__).parent.parent / "Test Files" / "May 2026.pdf"
REAL_PDF_AVAILABLE = REAL_PDF.exists()


# ---------------------------------------------------------------------------
# normalize_rentplus_description
# ---------------------------------------------------------------------------


class TestNormalizeRentplusDescription:
    """All whitespace and suffix variations should produce a canonical string."""

    def test_simple_description_unchanged(self):
        result = normalize_rentplus_description("48 West - Rent Plus Services")
        assert result == "48 West - Rent Plus Services"

    def test_newline_between_plus_and_services(self):
        result = normalize_rentplus_description(
            "Campus Creek Cottages - Rent Plus\nServices"
        )
        assert result == "Campus Creek Cottages - Rent Plus Services"

    def test_newline_between_rent_and_plus(self):
        result = normalize_rentplus_description(
            "The Bluff at Waterworks Landing - Rent\nPlus Services"
        )
        assert result == "The Bluff at Waterworks Landing - Rent Plus Services"

    def test_extra_spaces_around_hyphen(self):
        result = normalize_rentplus_description(
            "Campus Creek Cottages-  Rent Plus Services"
        )
        assert result == "Campus Creek Cottages - Rent Plus Services"

    def test_leading_and_trailing_spaces(self):
        result = normalize_rentplus_description(
            "  Station 42  -  Rent Plus  Services  "
        )
        assert result == "Station 42 - Rent Plus Services"

    def test_none_returns_empty_string(self):
        result = normalize_rentplus_description(None)
        assert result == ""

    def test_empty_string(self):
        result = normalize_rentplus_description("")
        assert result == ""

    def test_continuation_line_normalization(self):
        """Simulates description assembled from continuation PDF rows."""
        # Typical result when two table rows are merged: item row has the
        # property name, continuation row has "Rent Plus Services"
        merged = "The Finmore at 241 - Rent Plus Services"
        result = normalize_rentplus_description(merged)
        assert result == "The Finmore at 241 - Rent Plus Services"


# ---------------------------------------------------------------------------
# extract_rentplus_property_name
# ---------------------------------------------------------------------------


class TestExtractRentplusPropertyName:
    """Core property extraction: suffix removal and annotation stripping."""

    def test_single_line_standard(self):
        assert extract_rentplus_property_name(
            "48 West - Rent Plus Services"
        ) == "48 West"

    def test_newline_between_plus_and_services(self):
        assert extract_rentplus_property_name(
            "Campus Creek Cottages - Rent Plus\nServices"
        ) == "Campus Creek Cottages"

    def test_newline_between_rent_and_plus(self):
        assert extract_rentplus_property_name(
            "The Bluff at Waterworks Landing - Rent\nPlus Services"
        ) == "The Bluff at Waterworks Landing"

    def test_extra_spaces_around_hyphen(self):
        assert extract_rentplus_property_name(
            "Campus Creek Cottages-  Rent Plus Services"
        ) == "Campus Creek Cottages"

    def test_property_with_ampersand(self):
        assert extract_rentplus_property_name(
            "Hannah Townhomes & Lofts - Rent Plus Services"
        ) == "Hannah Townhomes & Lofts"

    def test_property_with_number(self):
        assert extract_rentplus_property_name(
            "The Finmore at 241 - Rent Plus Services"
        ) == "The Finmore at 241"

    def test_property_starting_with_the(self):
        assert extract_rentplus_property_name(
            "The Summit at Coates Run - Rent Plus Services"
        ) == "The Summit at Coates Run"

    def test_new_annotation_stripped(self):
        """'(new)' vendor annotation must be removed from the property name."""
        assert extract_rentplus_property_name(
            "The Summit at Coates Run (new) - Rent Plus Services"
        ) == "The Summit at Coates Run"

    def test_missing_suffix_raises_value_error(self):
        with pytest.raises(ValueError, match="service suffix"):
            extract_rentplus_property_name("Some Property Without A Suffix")

    def test_empty_description_raises_value_error(self):
        with pytest.raises(ValueError, match="empty"):
            extract_rentplus_property_name("")

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError, match="empty"):
            extract_rentplus_property_name(None)

    def test_case_insensitive_suffix(self):
        # "RENT PLUS SERVICES" should still match
        assert extract_rentplus_property_name(
            "Theory U District - RENT PLUS SERVICES"
        ) == "Theory U District"


# ---------------------------------------------------------------------------
# normalize_property_name_for_matching
# ---------------------------------------------------------------------------


class TestNormalizePropertyNameForMatching:
    def test_lowercased(self):
        assert normalize_property_name_for_matching("48 West") == "48 west"

    def test_ampersand_preserved(self):
        assert (
            normalize_property_name_for_matching("Hannah Townhomes & Lofts")
            == "hannah townhomes & lofts"
        )

    def test_numbers_preserved(self):
        assert (
            normalize_property_name_for_matching("The Finmore at 241")
            == "the finmore at 241"
        )

    def test_alphanumeric_start(self):
        assert normalize_property_name_for_matching("i5 Wynwood") == "i5 wynwood"

    def test_none_returns_empty_string(self):
        assert normalize_property_name_for_matching(None) == ""

    def test_curly_apostrophe_normalised(self):
        # NFKC + apostrophe replacement
        assert (
            normalize_property_name_for_matching("O\u2019Brien Properties")
            == "o'brien properties"
        )


# ---------------------------------------------------------------------------
# RentPlusPDFParser.parse_description (structured result)
# ---------------------------------------------------------------------------


class TestRentPlusPDFParserParseDescription:
    parser = RentPlusPDFParser()

    def test_success_returns_parsed_status(self):
        result = self.parser.parse_description("555 Boulevard - Rent Plus Services")
        assert result["property_parse_status"] == "parsed"
        assert result["extracted_property_name"] == "555 Boulevard"
        assert result["normalized_property_name"] == "555 boulevard"
        assert result["property_parse_exception"] is None

    def test_normalized_description_populated(self):
        result = self.parser.parse_description(
            "Campus Creek Cottages - Rent Plus\nServices"
        )
        assert result["normalized_description"] == (
            "Campus Creek Cottages - Rent Plus Services"
        )

    def test_failure_returns_failed_status(self):
        result = self.parser.parse_description("No Service Suffix Here")
        assert result["property_parse_status"] == "failed"
        assert result["extracted_property_name"] == ""
        assert result["normalized_property_name"] == ""
        assert result["property_parse_exception"] is not None

    def test_multiple_rate_rows_same_description(self):
        """Different rate rows for the same property all produce the same name."""
        descriptions = [
            "The Valley - Rent Plus Services",
            "The Valley - Rent Plus Services",
        ]
        names = [
            self.parser.parse_description(d)["extracted_property_name"]
            for d in descriptions
        ]
        assert names[0] == names[1] == "The Valley"


# ---------------------------------------------------------------------------
# Integration test (skipped when real PDF is absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_PDF_AVAILABLE,
    reason="Real PDF invoice (Test Files/May 2026.pdf) not available",
)
class TestMay2026InvoiceDescriptions:
    """Verify that the description parser extracts correct property names from
    the real May 2026 Rent Plus invoice."""

    @pytest.fixture(scope="class")
    @classmethod
    def lines(cls):
        from services.pdf_invoice_parser import parse_rent_plus_pdf
        invoice_lines, _ = parse_rent_plus_pdf(str(REAL_PDF), "2026-05")
        return invoice_lines

    def test_no_parse_failures(self, lines):
        failed = [ln for ln in lines if ln.property_parse_status == "failed"]
        failed_descriptions = [ln.original_description for ln in failed]
        assert failed == [], f"Parse failures: {failed_descriptions}"

    def test_all_lines_have_extracted_property_name(self, lines):
        missing = [ln for ln in lines if not ln.extracted_property_name]
        assert missing == []

    def test_key_properties_present(self, lines):
        extracted = {ln.extracted_property_name for ln in lines}
        expected_sample = [
            "48 West",
            "555 Boulevard",
            "Campus Creek Cottages",
            "Hannah Townhomes & Lofts",
            "The Summit at Coates Run",
            "The Bluff at Waterworks Landing",
            "The Finmore at 241",
            "i5 Wynwood",
        ]
        missing = [name for name in expected_sample if name not in extracted]
        assert missing == [], f"Expected properties not found in PDF: {missing}"

    def test_new_annotation_stripped_in_real_invoice(self, lines):
        """No property name in the parsed output should end with '(new)'."""
        new_annotation_names = [
            ln.extracted_property_name
            for ln in lines
            if ln.extracted_property_name.lower().endswith("(new)")
        ]
        assert new_annotation_names == [], (
            f"Property names still contain '(new)': {new_annotation_names}"
        )

    def test_normalized_property_name_is_lowercase(self, lines):
        mixed_case = [
            ln.normalized_property_name
            for ln in lines
            if ln.normalized_property_name != ln.normalized_property_name.lower()
        ]
        assert mixed_case == []
