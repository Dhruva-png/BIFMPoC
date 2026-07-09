import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import extractor
from app.utils.config_loader import get_fields_for_form


def _fake_pages(n):
    # Extraction only ever reads .stem/.parent off these - real image
    # files aren't needed since _extract_fields_from_page is mocked out.
    return [Path(f"/tmp/fake_page_{i}.png") for i in range(1, n + 1)]


def _run_and_capture_requested_pages(form_code, n_pages=3):
    """Runs _extract_standard_form with a mocked page-extraction call and
    returns the list of page images it was actually asked to read."""
    requested_pages = []

    def fake_extract(page_img, field_defs, form_label, fc=""):
        requested_pages.append(page_img)
        return {}, []

    with patch.object(extractor, "_extract_fields_from_page", side_effect=fake_extract):
        extractor._extract_standard_form(form_code, _fake_pages(n_pages))

    return requested_pages


def test_dis_scans_first_two_pages_not_just_the_cover_page():
    # DIS's page 1 is a cover/instructions page with no investor data or
    # fund table on it - page 2 is where the real form is. Only reading
    # page 1 (the old bug) means fund_name/fund_number/everything comes
    # back empty.
    pages = _run_and_capture_requested_pages("DIS")
    assert len(pages) == 2
    assert pages[0].name == "fake_page_1.png"
    assert pages[1].name == "fake_page_2.png"


def test_add_and_debit_also_scan_two_pages():
    for form_code in ("ADD", "DEBIT", "DIS_GSG"):
        pages = _run_and_capture_requested_pages(form_code)
        assert len(pages) == 2, f"{form_code} should scan 2 pages"


def test_kyc_only_needs_page_one():
    # KYC's own page 1 genuinely IS the form (no cover page on that
    # template) - no need to spend a second vision call on it.
    pages = _run_and_capture_requested_pages("KYC")
    assert len(pages) == 1
    assert pages[0].name == "fake_page_1.png"


def test_static_still_scans_two_pages():
    # Pre-existing behavior (Form A + Form B sections) must be unaffected.
    pages = _run_and_capture_requested_pages("STATIC")
    assert len(pages) == 2


def test_single_page_document_does_not_crash():
    # A document scanned with only 1 page shouldn't blow up asking for a
    # page that doesn't exist - list slicing just returns what's there.
    pages = _run_and_capture_requested_pages("DIS", n_pages=1)
    assert len(pages) == 1


def test_appform_defines_date_signed_field():
    # The handwritten "date signed" next to the signature block was never
    # captured for APPFORM (every other form type already asks for it) -
    # config/field_definitions.json must declare it so the extractor's
    # prompt actually asks the vision model to read it.
    field_ids = {f["id"] for f in get_fields_for_form("APPFORM")}
    assert "date_signed" in field_ids


def test_appform_date_signed_is_read_on_the_primary_page_not_guardian_only():
    # date_signed applies to every APPFORM submission, not just the
    # guardian/minor section on page 10 - it must be in the primary field
    # subset sent for page 1, not filtered out alongside GUARDIAN_FIELD_IDS.
    field_defs = get_fields_for_form("APPFORM")
    primary_fields = [
        f for f in field_defs
        if f.get("page_hint") != "Page 10" and f["id"] not in extractor.GUARDIAN_FIELD_IDS
    ]
    assert any(f["id"] == "date_signed" for f in primary_fields)


if __name__ == "__main__":
    test_dis_scans_first_two_pages_not_just_the_cover_page()
    test_add_and_debit_also_scan_two_pages()
    test_kyc_only_needs_page_one()
    test_static_still_scans_two_pages()
    test_single_page_document_does_not_crash()
    test_appform_defines_date_signed_field()
    test_appform_date_signed_is_read_on_the_primary_page_not_guardian_only()
    print("All page-selection tests passed.")
