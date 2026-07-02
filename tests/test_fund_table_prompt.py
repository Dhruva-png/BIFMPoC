import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.extractor import _build_field_prompt
from app.utils.config_loader import get_fields_for_form


def test_debit_prompt_gets_three_table_guidance_not_add_wording():
    fields = get_fields_for_form("DEBIT")
    prompt = _build_field_prompt(fields, "Debit Order Form", "DEBIT")
    # Must describe all three DEBIT sub-tables by their real column names...
    assert "Increase to (BWP)" in prompt
    assert "Decrease to (BWP)" in prompt
    assert "Existing amount" in prompt
    assert "Cancellation date" in prompt
    assert "New amount (BWP)" in prompt
    # ...and must NOT carry over ADD-only vocabulary that doesn't exist on DEBIT.
    assert "Lump sum deposit" not in prompt
    assert "Income Distribution" not in prompt


def test_add_prompt_keeps_single_table_wording():
    fields = get_fields_for_form("ADD")
    prompt = _build_field_prompt(fields, "Additional Investment Form", "ADD")
    assert "Income Distribution" in prompt
    assert "risk-appetite header" in prompt
    # ADD has one table, not DEBIT's three - shouldn't pull in cancel/change wording.
    assert "Cancellation date" not in prompt


def test_dis_prompt_mentions_percentage_as_an_amount_signal():
    fields = get_fields_for_form("DIS")
    prompt = _build_field_prompt(fields, "Disinvestment Form", "DIS")
    assert "percentage" in prompt.lower()


def test_no_fund_table_guidance_when_form_has_no_fund_table_fields():
    fields = get_fields_for_form("STATIC")
    prompt = _build_field_prompt(fields, "Static Form", "STATIC")
    assert "fund list table" not in prompt


if __name__ == "__main__":
    test_debit_prompt_gets_three_table_guidance_not_add_wording()
    test_add_prompt_keeps_single_table_wording()
    test_dis_prompt_mentions_percentage_as_an_amount_signal()
    test_no_fund_table_guidance_when_form_has_no_fund_table_fields()
    print("All fund-table prompt tests passed.")
