import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import ExtractionResult, FieldValue
from app.services.consolidator import backfill_from_profile, build_person_profile
from app.validation.engine import validate


def _fv(fid, value, confidence=90.0):
    return FieldValue(field_id=fid, value=value, confidence=confidence)


def test_backfill_fills_blank_mandatory_fields_from_sibling_documents():
    # STATIC form has entity_number + full_name + contact_number filled in
    static = ExtractionResult(
        source_file="STATIC - AMOLEMO.pdf",
        form_code="STATIC",
        fields={
            "entity_number": _fv("entity_number", "381346789"),
            "full_name": _fv("full_name", "K Amolemo"),
            "contact_number": _fv("contact_number", "71234567"),
            "email": _fv("email", "k.amolemo@example.com"),
            "change_type": _fv("change_type", "Contact Details"),
        },
    )
    # DIS form is missing entity_number/full_name/contact_number/email —
    # the investor assumed the office already had them from the Static form.
    dis = ExtractionResult(
        source_file="DIS - AMOLEMO.pdf",
        form_code="DIS",
        fields={
            "fund_name": _fv("fund_name", "BIFM Balanced Fund"),
            "disinvestment_amount": _fv("disinvestment_amount", "5000"),
        },
    )

    profile = build_person_profile([static, dis])
    assert profile["entity_number"].value == "381346789"
    assert profile["full_name"].value == "K Amolemo"

    backfilled = backfill_from_profile(dis, profile)
    assert set(backfilled) == {"entity_number", "full_name", "contact_number", "email"}
    assert dis.field_value("entity_number") == "381346789"
    assert dis.field_value("full_name") == "K Amolemo"
    assert dis.fields["entity_number"].source == "backfilled:STATIC - AMOLEMO.pdf"

    # Instruction-specific field must NEVER be pulled from a sibling doc.
    assert "disinvestment_amount" not in backfilled
    assert "change_type" not in dis.fields  # STATIC-only field never injected into DIS

    # Validation should now pass the previously-missing mandatory fields.
    report = validate(dis)
    failed_ids = {r.field_id for r in report.results if r.status.value == "FAIL"}
    assert "entity_number" not in failed_ids
    assert "full_name" not in failed_ids
    assert "contact_number" not in failed_ids


def test_backfill_never_overwrites_an_existing_value():
    a = ExtractionResult(
        source_file="A.pdf", form_code="ADD",
        fields={"full_name": _fv("full_name", "Correct Name")},
    )
    b = ExtractionResult(
        source_file="B.pdf", form_code="DEBIT",
        fields={"full_name": _fv("full_name", "Different Name")},
    )
    profile = build_person_profile([a, b])
    backfill_from_profile(a, profile)
    assert a.field_value("full_name") == "Correct Name"  # untouched


if __name__ == "__main__":
    test_backfill_fills_blank_mandatory_fields_from_sibling_documents()
    test_backfill_never_overwrites_an_existing_value()
    print("All consolidator tests passed.")
