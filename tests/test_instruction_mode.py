import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import FieldValue
from app.services.extractor import _derive_metadata


def _fv(fid, value, confidence=90.0):
    return FieldValue(field_id=fid, value=value, confidence=confidence)


def _mode(fields: dict) -> str:
    derived = _derive_metadata("DIS", fields)
    return derived["instruction_mode"].value


def test_full_closure_wins_even_with_stray_amount():
    # Closure tick takes priority over anything left in the amount/percent boxes.
    fields = {
        "account_closure": _fv("account_closure", "Yes"),
        "disinvestment_amount": _fv("disinvestment_amount", "500"),
        "fund_name": _fv("fund_name", "BIFM Balanced Fund"),
    }
    assert _mode(fields) == "Full Closure"


def test_full_closure_boolean_true():
    fields = {"account_closure": _fv("account_closure", True)}
    assert _mode(fields) == "Full Closure"


def test_full_closure_alternate_checkbox_spellings():
    for token in ("checked", "ticked", "X", "1", "on"):
        fields = {"account_closure": _fv("account_closure", token)}
        assert _mode(fields) == "Full Closure", f"token={token!r} should register as ticked"


def test_percentage_mode():
    fields = {
        "account_closure": _fv("account_closure", "No"),
        "disinvestment_percentage": _fv("disinvestment_percentage", "50"),
    }
    assert _mode(fields) == "Percentage"


def test_partial_withdrawal_mode_from_amount_column():
    fields = {
        "account_closure": _fv("account_closure", False),
        "disinvestment_amount": _fv("disinvestment_amount", "5000"),
    }
    assert _mode(fields) == "Partial Withdrawal"


def test_unknown_when_nothing_present():
    fields = {"fund_name": _fv("fund_name", "BIFM Balanced Fund")}
    assert _mode(fields) == "Unknown"


def test_blank_placeholder_amount_does_not_count():
    # "N/A" written in the amount column must not be mistaken for a real amount.
    fields = {
        "disinvestment_amount": _fv("disinvestment_amount", "N/A"),
        "disinvestment_percentage": _fv("disinvestment_percentage", ""),
    }
    assert _mode(fields) == "Unknown"


def test_applies_to_dis_gsg_too():
    derived = _derive_metadata("DIS_GSG", {"account_closure": _fv("account_closure", "Yes")})
    assert derived["instruction_mode"].value == "Full Closure"


def test_real_world_dis_fund_table_example():
    # Exact values from the client's sample DIS fund table: Fund Number
    # 1674982 | Bifm Pula Money Market Fund | Amount 200.00 | Percentage
    # blank. Amount with no percentage and no closure tick = Partial
    # Withdrawal, and the fund name (Money Market) drives fund_category.
    fields = {
        "fund_number": _fv("fund_number", "1674982"),
        "fund_name": _fv("fund_name", "Bifm Pula Money Market Fund"),
        "disinvestment_amount": _fv("disinvestment_amount", "200.00"),
    }
    derived = _derive_metadata("DIS", fields)
    assert derived["instruction_mode"].value == "Partial Withdrawal"
    assert derived["fund_category"].value == "Money Market"


if __name__ == "__main__":
    test_full_closure_wins_even_with_stray_amount()
    test_full_closure_boolean_true()
    test_full_closure_alternate_checkbox_spellings()
    test_percentage_mode()
    test_partial_withdrawal_mode_from_amount_column()
    test_unknown_when_nothing_present()
    test_blank_placeholder_amount_does_not_count()
    test_applies_to_dis_gsg_too()
    test_real_world_dis_fund_table_example()
    print("All instruction_mode tests passed.")
