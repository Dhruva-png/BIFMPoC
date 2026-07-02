import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import ExtractionResult, FieldValue
from app.services.consolidator import backfill_from_profile, build_person_profile
from app.utils.confidence import needs_recheck


def _fv(fid, value, confidence=90.0):
    return FieldValue(field_id=fid, value=value, confidence=confidence)


def _doc(name, form_code, **fields):
    return ExtractionResult(
        source_file=name,
        form_code=form_code,
        fields={fid: _fv(fid, val) for fid, val in fields.items()},
    )


def test_phone_numbers_with_different_formatting_still_form_a_majority():
    # Same number, three different handwriting/formatting styles - previously
    # these grouped as three different values of 1 each, so the "majority"
    # was really just whichever document happened to have the highest
    # per-document confidence score.
    docs = [
        _doc("A.pdf", "STATIC", contact_number="71234567"),
        _doc("B.pdf", "DIS", contact_number="071 234 567"),
        _doc("C.pdf", "ADD", contact_number="+267 71-234-567"),
        _doc("D.pdf", "DEBIT", contact_number="79999999"),  # genuine outlier
    ]
    profile = build_person_profile(docs)
    # All three equivalent phone spellings should win as one 3-vs-1 majority.
    assert profile["contact_number"].value in ("71234567", "071 234 567", "+267 71-234-567")
    assert "3/4" in profile["contact_number"].agreement


def test_dates_in_different_formats_still_form_a_majority():
    docs = [
        _doc("A.pdf", "APPFORM", date_of_birth="1990-05-14"),
        _doc("B.pdf", "KYC", date_of_birth="14/05/1990"),
        _doc("C.pdf", "STATIC", date_of_birth="14 May 1990"),
    ]
    profile = build_person_profile(docs)
    assert "3/3" in profile["date_of_birth"].agreement


def test_currency_free_amounts_are_not_shareable_but_names_with_spacing_group_correctly():
    docs = [
        _doc("A.pdf", "STATIC", full_name="K.  Amolemo"),
        _doc("B.pdf", "DIS", full_name=" k amolemo "),
        _doc("C.pdf", "ADD", full_name="K Amolemo"),
    ]
    profile = build_person_profile(docs)
    assert "3/3" in profile["full_name"].agreement


def test_id_numbers_with_spacing_or_dashes_still_form_a_majority():
    docs = [
        _doc("A.pdf", "APPFORM", id_number="381 346 789"),
        _doc("B.pdf", "KYC", id_number="381-346-789"),
        _doc("C.pdf", "STATIC", id_number="381346789"),
    ]
    profile = build_person_profile(docs)
    assert "3/3" in profile["id_number"].agreement


def test_genuine_disagreement_still_reported_as_disagreement():
    # A 1-1 split is a genuine tie, not a majority - must be flagged as
    # unresolved (NO MAJORITY), not mislabeled as "majority vote".
    docs = [
        _doc("A.pdf", "STATIC", full_name="John Smith"),
        _doc("B.pdf", "DIS", full_name="Jane Smith"),
    ]
    profile = build_person_profile(docs)
    assert "NO MAJORITY" in profile["full_name"].agreement
    assert "2-way tie" in profile["full_name"].agreement


def test_three_way_tie_is_not_backfilled():
    # The exact bug reported: three documents each give a DIFFERENT value
    # for the same field (e.g. three different account numbers). There is
    # no majority - picking one via confidence tie-break and silently
    # backfilling it elsewhere is wrong; it must be flagged for manual
    # review and NOT propagated onto blank fields on other documents.
    docs = [
        _doc("KYC.pdf", "KYC", account_number="1078631"),
        _doc("DIS.pdf", "DIS", account_number="1033921"),
        _doc("DEBIT.pdf", "DEBIT", account_number="1073421"),
    ]
    profile = build_person_profile(docs)
    assert "NO MAJORITY" in profile["account_number"].agreement
    assert "3-way tie" in profile["account_number"].agreement
    assert needs_recheck(profile["account_number"].confidence)

    # A sibling ADD document with a blank account_number must NOT get one
    # of these three tied values silently injected.
    add_doc = _doc("ADD.pdf", "ADD")
    backfilled = backfill_from_profile(add_doc, profile)
    assert "account_number" not in backfilled
    assert "account_number" not in add_doc.fields


def test_real_majority_over_a_tie_still_wins_and_is_backfilled():
    # 2 documents agree, 1 disagrees - this IS a real majority (2 > 1) and
    # should behave exactly as before: labeled as majority vote, and safe
    # to backfill onto a blank sibling field.
    docs = [
        _doc("A.pdf", "KYC", email="right@example.com"),
        _doc("B.pdf", "DEBIT", email="right@example.com"),
        _doc("C.pdf", "ADD", email="wrong@example.com"),
    ]
    profile = build_person_profile(docs)
    assert "majority vote: 2/3" in profile["email"].agreement
    assert profile["email"].value == "right@example.com"

    dis_doc = _doc("D.pdf", "DIS")
    backfilled = backfill_from_profile(dis_doc, profile)
    assert "email" in backfilled
    assert dis_doc.fields["email"].value == "right@example.com"


if __name__ == "__main__":
    test_phone_numbers_with_different_formatting_still_form_a_majority()
    test_dates_in_different_formats_still_form_a_majority()
    test_currency_free_amounts_are_not_shareable_but_names_with_spacing_group_correctly()
    test_id_numbers_with_spacing_or_dashes_still_form_a_majority()
    test_genuine_disagreement_still_reported_as_disagreement()
    test_three_way_tie_is_not_backfilled()
    test_real_majority_over_a_tie_still_wins_and_is_backfilled()
    print("All majority-vote normalization tests passed.")
