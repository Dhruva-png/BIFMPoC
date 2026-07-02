import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import extractor


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


if __name__ == "__main__":
    test_dis_scans_first_two_pages_not_just_the_cover_page()
    test_add_and_debit_also_scan_two_pages()
    test_kyc_only_needs_page_one()
    test_static_still_scans_two_pages()
    test_single_page_document_does_not_crash()
    print("All page-selection tests passed.")
