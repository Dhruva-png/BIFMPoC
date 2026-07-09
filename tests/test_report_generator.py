import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import ExtractionResult, FieldValue, ProcessingLogEntry, ValidationReport
from app.services.report_generator import ExcelReportBuilder, _format_detail


def _fv(**kwargs):
    defaults = dict(field_id="x", value="v", confidence=92.0)
    defaults.update(kwargs)
    return FieldValue(**defaults)


def _log_entry(**overrides):
    defaults = dict(
        original_filename="DIS - AMOLEMO.pdf",
        new_filename="DIS_E1_AMOLEMO_20260709.pdf",
        form_type_detected="DIS",
        classification_confidence=95.0,
        validation_status="PASS",
        destination_path="/tmp/out",
        instruction_status="Captured",
    )
    defaults.update(overrides)
    return ProcessingLogEntry.now(**defaults)


def test_plain_extraction_names_its_own_document():
    fv = _fv(source="extracted", source_page=3)
    detail = _format_detail(fv, own_source_file="DIS - AMOLEMO.pdf")
    assert "DIS - AMOLEMO.pdf" in detail
    assert "p.3" in detail
    assert "92% confidence" in detail


def test_plain_extraction_without_own_source_file_falls_back():
    fv = _fv(source="extracted")
    detail = _format_detail(fv)
    assert "this document" in detail


def test_backfilled_names_the_sibling_document():
    fv = _fv(source="backfilled:STATIC - AMOLEMO.pdf")
    detail = _format_detail(fv)
    assert "STATIC - AMOLEMO.pdf" in detail
    assert "backfilled" in detail


def test_consolidated_profile_value_names_the_winning_document():
    fv = _fv(source="DIS - AMOLEMO.pdf", agreement="3/4 documents agree")
    detail = _format_detail(fv)
    assert "DIS - AMOLEMO.pdf" in detail
    assert "3/4 documents agree" in detail


def test_none_field_value_is_blank():
    assert _format_detail(None) == ""


def test_add_form_excludes_backfilled_fields_from_individual_document_views():
    # A field backfilled in from a sibling document in the same batch must
    # not appear as if it were read off THIS document's own row/sheet.
    builder = ExcelReportBuilder()
    extraction = ExtractionResult(
        source_file="DIS - AMOLEMO.pdf",
        form_code="DIS",
        fields={
            "fund_name": _fv(field_id="fund_name", value="BIFM Balanced Fund", source="extracted"),
            "full_name": _fv(
                field_id="full_name", value="K Amolemo", confidence=85.0,
                source="backfilled:STATIC - AMOLEMO.pdf",
            ),
        },
    )
    validation = ValidationReport(source_file="DIS - AMOLEMO.pdf", entity_number="E1", results=[])
    builder.add_form(extraction, validation, _log_entry())

    investor_row = builder._investor_rows[0]
    assert "full_name" not in investor_row
    assert investor_row["fund_name"] == "BIFM Balanced Fund"

    field_values = builder._investor_field_values[0]
    assert "full_name" not in field_values
    assert "fund_name" in field_values

    confidence_fields = {row["field"] for row in builder._confidence_rows}
    assert "full_name" not in confidence_fields
    assert "fund_name" in confidence_fields


def test_consolidated_profile_still_shows_backfilled_style_merged_data():
    # The batch-wide Consolidated Investor Profile sheet is explicitly the
    # merged view - it must still carry every profile field regardless of
    # which document each value ultimately came from.
    builder = ExcelReportBuilder()
    profile = {
        "full_name": _fv(field_id="full_name", value="K Amolemo", agreement="2/2 document(s) agree"),
    }
    builder.add_consolidated_profile(profile, person_key="Amolemo", source_files=["A.pdf", "B.pdf"])
    assert builder._consolidated_rows[0]["full_name"] == "K Amolemo"
    assert builder._consolidated_field_values[0]["full_name"].agreement == "2/2 document(s) agree"


if __name__ == "__main__":
    test_plain_extraction_names_its_own_document()
    test_plain_extraction_without_own_source_file_falls_back()
    test_backfilled_names_the_sibling_document()
    test_consolidated_profile_value_names_the_winning_document()
    test_none_field_value_is_blank()
    test_add_form_excludes_backfilled_fields_from_individual_document_views()
    test_consolidated_profile_still_shows_backfilled_style_merged_data()
    print("All report_generator detail tests passed.")
