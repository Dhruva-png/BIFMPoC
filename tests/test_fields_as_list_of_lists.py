import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import ExtractionResult, FieldValue
from app.utils.field_export import fields_as_list_of_lists


def _fv(fid, value, confidence=90.0):
    return FieldValue(field_id=fid, value=value, confidence=confidence)


def test_converts_extraction_fields_to_list_of_lists():
    extraction = ExtractionResult(
        source_file="test.pdf",
        form_code="DIS",
        fields={
            "full_name": _fv("full_name", "Dhruva"),
            "age": _fv("age", 25),
        },
    )
    result = fields_as_list_of_lists(extraction)
    assert result == [["full_name", "Dhruva"], ["age", 25]]
    assert isinstance(result, list)
    assert all(isinstance(pair, list) for pair in result)


def test_preserves_native_value_types_not_stringified():
    extraction = ExtractionResult(
        source_file="test.pdf",
        form_code="DIS",
        fields={"amount": _fv("amount", 200.0), "closed": _fv("closed", True)},
    )
    result = fields_as_list_of_lists(extraction)
    values = dict(result)
    assert values["amount"] == 200.0 and isinstance(values["amount"], float)
    assert values["closed"] is True


def test_includes_derived_fields_too():
    extraction = ExtractionResult(
        source_file="test.pdf",
        form_code="DIS",
        fields={
            "fund_name": _fv("fund_name", "Bifm Pula Money Market Fund"),
            "instruction_mode": _fv("instruction_mode", "Partial Withdrawal"),
        },
    )
    result = fields_as_list_of_lists(extraction)
    field_ids = [pair[0] for pair in result]
    assert "fund_name" in field_ids
    assert "instruction_mode" in field_ids


def test_none_extraction_returns_empty_list():
    assert fields_as_list_of_lists(None) == []


def test_empty_fields_returns_empty_list():
    extraction = ExtractionResult(source_file="test.pdf", form_code="DIS", fields={})
    assert fields_as_list_of_lists(extraction) == []


if __name__ == "__main__":
    test_converts_extraction_fields_to_list_of_lists()
    test_preserves_native_value_types_not_stringified()
    test_includes_derived_fields_too()
    test_none_extraction_returns_empty_list()
    test_empty_fields_returns_empty_list()
    print("All fields_as_list_of_lists tests passed.")
