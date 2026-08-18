"""
Tests for the PDF invoice parser.

Tests that can run without the real invoice use synthetic fixture data.
Tests against the real PDF file (Test Files/May 2026.pdf) are skipped
automatically when the file is not present, so they pass cleanly in CI
or on machines without the confidential file.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from services.pdf_invoice_parser import (
    RentPlusPDFParser,
    _extract_line_item_rows,
    _extract_invoice_total,
    get_pdf_parser,
)

REAL_PDF = Path(__file__).parent.parent / "Test Files" / "May 2026.pdf"
REAL_PDF_AVAILABLE = REAL_PDF.exists()

# ---------------------------------------------------------------------------
# Property name extraction (no PDF needed)
# ---------------------------------------------------------------------------


class TestRentPlusPropertyNameExtraction:
    """Unit tests for the description → property name conversion."""

    parser = RentPlusPDFParser()

    def test_standard_suffix_stripped(self):
        result = self.parser.extract_property_name("Cobalt Row - Rent Plus Services")
        assert result == "Cobalt Row"

    def test_embedded_newline_in_suffix(self):
        # pdfplumber sometimes wraps "Rent Plus\nServices"
        result = self.parser.extract_property_name(
            "Campus Creek Townhomes - Rent Plus\nServices"
        )
        assert result == "Campus Creek Townhomes"

    def test_long_name_with_newline_in_suffix(self):
        # (new) annotation is stripped by the new RENTPLUS_TRAILING_PROPERTY_ANNOTATIONS rule
        result = self.parser.extract_property_name(
            "The Summit at Coates Run (new) - Rent\nPlus Services"
        )
        assert result == "The Summit at Coates Run"

    def test_no_suffix_returns_full_description(self):
        # If the suffix is absent, return the description as-is
        result = self.parser.extract_property_name("Some Random Description")
        assert result == "Some Random Description"

    def test_extra_whitespace_normalised(self):
        result = self.parser.extract_property_name(
            "  Station 42  -  Rent Plus  Services  "
        )
        assert result == "Station 42"


class TestGetPDFParser:
    def test_rent_plus_returns_correct_class(self):
        parser = get_pdf_parser("Rent Plus")
        assert isinstance(parser, RentPlusPDFParser)

    def test_unknown_vendor_returns_base(self):
        from services.pdf_invoice_parser import BasePDFInvoiceParser
        parser = get_pdf_parser("Unknown Vendor XYZ")
        assert isinstance(parser, BasePDFInvoiceParser)

    def test_case_insensitive(self):
        parser = get_pdf_parser("RENT PLUS")
        assert isinstance(parser, RentPlusPDFParser)


# ---------------------------------------------------------------------------
# Real-PDF tests (skipped if file is absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_PDF_AVAILABLE,
    reason="Real PDF invoice (Test Files/May 2026.pdf) not available",
)
class TestRealPDFParsing:
    """Integration tests against the actual May 2026 Rent Plus invoice."""

    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        from services.pdf_invoice_parser import parse_rent_plus_pdf
        lines, summary = parse_rent_plus_pdf(str(REAL_PDF), "2026-05")
        return lines, summary

    def test_returns_lines(self, result):
        lines, _ = result
        assert len(lines) > 0

    def test_returns_summary(self, result):
        _, summary = result
        assert summary is not None

    def test_source_invoice_total_extracted(self, result):
        _, summary = result
        # AMOUNT DUE from the remittance page
        assert summary.source_invoice_total == Decimal("109790.25")

    def test_all_lines_have_property_name(self, result):
        lines, _ = result
        for line in lines:
            assert line.normalized_property_name, (
                f"Row {line.source_row} has empty property name "
                f"(description: {line.original_description!r})"
            )

    def test_no_vendor_property_ids_in_pdf(self, result):
        """PDF format has no vendor property ID column."""
        lines, _ = result
        assert all(line.vendor_property_id == "" for line in lines)

    def test_vendor_set_on_all_lines(self, result):
        lines, _ = result
        assert all(line.vendor == "Rent Plus" for line in lines)

    def test_reporting_month_set(self, result):
        lines, _ = result
        assert all(line.reporting_month == "2026-05" for line in lines)

    def test_line_amounts_are_decimal(self, result):
        lines, _ = result
        for line in lines:
            assert isinstance(line.line_amount, Decimal)
            assert isinstance(line.unit_price, Decimal)
            assert isinstance(line.original_quantity, Decimal)

    def test_reversal_lines_have_negative_amounts(self, result):
        lines, _ = result
        reversals = [ln for ln in lines if ln.unit_price < Decimal("0")]
        assert len(reversals) > 0, "Expected some reversal lines in this invoice"
        for ln in reversals:
            assert ln.line_amount < Decimal("0"), (
                f"Reversal line row {ln.source_row} has positive amount: {ln.line_amount}"
            )

    def test_multiple_properties_present(self, result):
        lines, summary = result
        # The May 2026 invoice covers many properties
        assert summary.property_count >= 10

    def test_calculated_total_matches_source_total(self, result):
        _, summary = result
        assert abs(summary.invoice_difference) <= Decimal("0.02"), (
            f"Invoice total mismatch: source={summary.source_invoice_total} "
            f"calculated={summary.calculated_invoice_total} "
            f"diff={summary.invoice_difference}"
        )

    def test_known_property_present(self, result):
        """Spot-check: '48 West' should be in the parsed lines."""
        lines, _ = result
        names = {ln.normalized_property_name for ln in lines}
        assert "48 west" in names, f"'48 West' not found.  Parsed names: {sorted(names)[:10]}"

    def test_invoice_id_is_set(self, result):
        lines, summary = result
        assert summary.invoice_id.startswith("INV-")
        assert all(ln.invoice_id == summary.invoice_id for ln in lines)
