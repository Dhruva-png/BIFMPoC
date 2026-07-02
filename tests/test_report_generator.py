import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import FieldValue
from app.services.report_generator import _format_detail


def _fv(**kwargs):
    defaults = dict(field_id="x", value="v", confidence=92.0)
    defaults.update(kwargs)
    return FieldValue(**defaults)


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


if __name__ == "__main__":
    test_plain_extraction_names_its_own_document()
    test_plain_extraction_without_own_source_file_falls_back()
    test_backfilled_names_the_sibling_document()
    test_consolidated_profile_value_names_the_winning_document()
    test_none_field_value_is_blank()
    print("All report_generator detail tests passed.")
