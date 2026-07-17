"""
Tests for the failure-gated mandatory-field retry in app.services.extractor.

The contract that keeps this cheap: a document whose mandatory fields all
extracted cleanly costs ZERO extra vision calls, and a document with
missing mandatory data costs exactly ONE more - a focused re-read listing
only the missing fields, aimed at the page the form's data actually lives
on (page 2 for standard forms, whose page 1 is a cover page).
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import FieldValue
from app.services import extractor


def _fv(fid, value, confidence=90.0):
    return FieldValue(field_id=fid, value=value, confidence=confidence)


def _fake_pages(n):
    return {i: Path(f"/tmp/page_{i:03d}.png") for i in range(1, n + 1)}


# DIS mandatory fields: entity_number, full_name, contact_number, fund_name.
_DIS_COMPLETE = {
    "entity_number": _fv("entity_number", "38126341"),
    "full_name": _fv("full_name", "Nnelo Taunyane"),
    "contact_number": _fv("contact_number", "71890757"),
    "fund_name": _fv("fund_name", "BIFM Pula Money Market Fund"),
}


def test_no_retry_when_mandatory_fields_all_extracted():
    calls = []

    def fake_extract(page_img, field_defs, form_label, fc=""):
        calls.append((page_img, [f["id"] for f in field_defs]))
        return dict(_DIS_COMPLETE), []

    with patch.object(extractor, "_extract_fields_from_page", side_effect=fake_extract):
        extractor._extract_standard_form("DIS", _fake_pages(2))

    # Pages 1 and 2 read once each - no third call on a clean document.
    assert len(calls) == 2


def test_missing_mandatory_fields_trigger_one_focused_reread_of_page_two():
    calls = []

    def fake_extract(page_img, field_defs, form_label, fc=""):
        calls.append((page_img, [f["id"] for f in field_defs]))
        if len(calls) <= 2:
            # First pass: only the entity number was legible.
            return {"entity_number": _fv("entity_number", "38126341")}, []
        # The focused retry finds the rest.
        return {
            "full_name": _fv("full_name", "Nnelo Taunyane", 72.0),
            "contact_number": _fv("contact_number", "71890757", 70.0),
            "fund_name": _fv("fund_name", "BIFM Pula Money Market Fund", 75.0),
        }, []

    with patch.object(extractor, "_extract_fields_from_page", side_effect=fake_extract):
        result = extractor._extract_standard_form("DIS", _fake_pages(2))

    assert len(calls) == 3  # 2 page reads + exactly 1 retry
    retry_page, retry_field_ids = calls[2]
    # Retry targets page 2 (the form body - page 1 is the cover page) and
    # asks ONLY for the fields that were missing, nothing else.
    assert retry_page.name == "page_002.png"
    assert sorted(retry_field_ids) == ["contact_number", "full_name", "fund_name"]
    # Recovered values land in the final extraction.
    assert result.field_value("full_name") == "Nnelo Taunyane"
    assert result.field_value("contact_number") == "71890757"


def test_retry_that_still_finds_nothing_leaves_fields_missing():
    calls = []

    def fake_extract(page_img, field_defs, form_label, fc=""):
        calls.append(page_img)
        return {}, []

    with patch.object(extractor, "_extract_fields_from_page", side_effect=fake_extract):
        result = extractor._extract_standard_form("DIS", _fake_pages(2))

    assert len(calls) == 3  # the one retry fired, found nothing, and stopped
    assert result.field_value("full_name") is None  # still missing - validation will flag it


def test_retry_can_be_disabled_via_settings():
    calls = []

    def fake_extract(page_img, field_defs, form_label, fc=""):
        calls.append(page_img)
        return {}, []

    with patch.object(extractor, "_extract_fields_from_page", side_effect=fake_extract), \
         patch.object(extractor.settings, "retry_missing_mandatory", False):
        extractor._extract_standard_form("DIS", _fake_pages(2))

    assert len(calls) == 2  # no retry even though everything is missing
