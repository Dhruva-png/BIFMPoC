"""
Tests for app.utils.field_repair - the deterministic domain-constraint
layer that replaced multi-pass ("read the page twice and compare")
extraction.

The safety properties matter more than the repairs themselves: a layer
that silently turns a wrong read into a *plausible-looking* wrong value is
worse than no layer at all, because it launders a bad value into one that
passes validation. So the tests below pin down just as hard what it must
NOT do (pad, truncate, guess between ambiguous candidates) as what it
should fix.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import FieldValue
from app.utils import field_repair as fr
from app.utils.field_validators import apply_field_validation

OMANG = {"id_type": "Omang"}
OCCUPATION = {"options": ["Salaried employee", "Self-employed", "Minor Scholar",
                          "Student", "Retired", "Unemployed"]}
ACCOUNT_TYPE = {"options": ["Individual", "Joint", "Motshelo", "Acting on Behalf of"]}


# --------------------------------------------------------------- ID numbers

def test_omang_already_valid_is_untouched():
    value, outcome = fr.repair_field("id_number", "123450789", {}, OMANG)
    assert (value, outcome) == ("123450789", fr.OK)


def test_omang_letter_digit_confusion_is_repaired():
    # The single most common handwriting misread: O for 0, l for 1.
    value, outcome = fr.repair_field("id_number", "l2345O789", {}, OMANG)
    assert (value, outcome) == ("123450789", fr.REPAIRED)


def test_omang_separators_are_stripped_without_being_called_a_repair():
    value, outcome = fr.repair_field("id_number", "123 450-789", {}, OMANG)
    assert (value, outcome) == ("123450789", fr.OK)


def test_too_short_omang_is_never_padded():
    # An 8-digit Omang is genuinely wrong. Inventing a 9th digit would
    # launder it past the 9-digit validation rule - it must stay wrong and
    # visible.
    value, outcome = fr.repair_field("id_number", "12345678", {}, OMANG)
    assert value == "12345678"
    assert outcome == fr.UNREPAIRABLE


def test_too_long_omang_is_never_truncated():
    value, outcome = fr.repair_field("id_number", "1234507890", {}, OMANG)
    assert value == "1234507890"
    assert outcome == fr.UNREPAIRABLE


def test_passport_has_no_fixed_length_constraint():
    # Passport formats vary by country of issue - there's no domain to
    # check against, so the layer must not touch it.
    value, outcome = fr.repair_field("id_number", "AB123456", {}, {"id_type": "Passport"})
    assert (value, outcome) == ("AB123456", fr.NOT_APPLICABLE)


def test_id_number_untouched_when_id_type_unknown():
    # Without id_type we can't know whether 9 digits is even the right
    # rule, so no repair may be attempted.
    value, outcome = fr.repair_field("id_number", "12345O789", {}, {})
    assert outcome == fr.NOT_APPLICABLE


def test_branch_code_is_six_digits():
    assert fr.repair_field("branch_code", "06276S", {}, {}) == ("062765", fr.REPAIRED)
    assert fr.repair_field("branch_code", "062765", {}, {}) == ("062765", fr.OK)
    # Too short: left alone, not padded.
    assert fr.repair_field("branch_code", "1234", {}, {})[1] == fr.UNREPAIRABLE


def test_value_with_real_letters_is_not_mangled_into_digits():
    # "NOTANID" is all letters, several of them confusable. Repairing it
    # into digits would be nonsense - it must be rejected outright.
    value, outcome = fr.repair_field("id_number", "NOTANID", {}, OMANG)
    assert value == "NOTANID"
    assert outcome == fr.UNREPAIRABLE


# ------------------------------------------------------------------- enums

def test_enum_exact_match_is_ok():
    assert fr.repair_field("occupation_type", "Student", OCCUPATION, {}) == ("Student", fr.OK)


def test_enum_partial_read_snaps_to_the_full_option():
    # The model commonly returns the first word of a ticked option.
    value, outcome = fr.repair_field("occupation_type", "Salaried", OCCUPATION, {})
    assert (value, outcome) == ("Salaried employee", fr.REPAIRED)


def test_enum_casing_snaps_to_the_canonical_option():
    value, outcome = fr.repair_field("account_type", "acting on behalf of", ACCOUNT_TYPE, {})
    assert (value, outcome) == ("Acting on Behalf of", fr.REPAIRED)


def test_enum_typo_snaps_to_nearest_option():
    assert fr.repair_field("account_type", "Motshello", ACCOUNT_TYPE, {}) == ("Motshelo", fr.REPAIRED)


def test_enum_value_outside_the_domain_is_flagged_not_forced():
    # Nothing close - must NOT be snapped to an arbitrary option.
    value, outcome = fr.repair_field("occupation_type", "Astronaut", OCCUPATION, {})
    assert value == "Astronaut"
    assert outcome == fr.UNREPAIRABLE


def test_ambiguous_enum_is_left_alone_rather_than_guessed():
    # "Curront" is equidistant from both options (0.857 similarity to each),
    # so there is no single right answer - snapping either way would be a
    # coin flip presented to Ops as a fact. It must stay unrepaired and get
    # flagged for a human instead.
    ambiguous = {"options": ["Current", "Currant"]}
    value, outcome = fr.repair_field("account_type_banking", "Curront", ambiguous, {})
    assert value == "Curront"
    assert outcome == fr.UNREPAIRABLE


def test_clear_nearest_option_still_wins_when_another_option_merely_shares_a_prefix():
    # Contrast with the ambiguous case above: "Retire" is far closer to
    # "Retired" (0.923) than to "Retired (early)" (0.571), so there IS a
    # single right answer and it should be taken.
    options = {"options": ["Retired", "Retired (early)"]}
    value, outcome = fr.repair_field("occupation_type", "Retire", options, {})
    assert (value, outcome) == ("Retired", fr.REPAIRED)


# ------------------------------------------------------------------- funds

def test_fund_name_typo_snaps_to_a_real_bifm_fund():
    value, outcome = fr.repair_field("fund_name", "Bifm Pula Money Market Fnd", {}, {})
    assert "Money Market" in value
    assert outcome == fr.REPAIRED


def test_fund_name_lock_in_markers_are_stripped_before_matching():
    value, outcome = fr.repair_field("fund_name", "** Bifm Global Sustainable Growth Fund", {}, {})
    assert not value.startswith("*")
    assert "Global Sustainable Growth" in value


def test_unknown_fund_is_not_snapped_to_a_real_one():
    value, outcome = fr.repair_field("fund_name", "Some Unrelated Fund", {}, {})
    assert value == "Some Unrelated Fund"
    assert outcome == fr.UNREPAIRABLE


# ------------------------------------------------- integration + confidence

def test_repair_runs_before_numeric_normalization_strips_the_evidence():
    # Regression guard: field_validators' numeric-ID normalizer strips
    # non-digits. If it ran first it would turn "12345O789" into an
    # 8-digit "12345789" and the O/0 confusion would be unrecoverable.
    fields = {
        "id_type": FieldValue(field_id="id_type", value="Omang", confidence=90.0),
        "id_number": FieldValue(field_id="id_number", value="12345O789", confidence=70.0),
    }
    apply_field_validation("APPFORM", fields)
    assert fields["id_number"].value == "123450789"


def test_domain_violation_lowers_confidence_despite_a_confident_model():
    # A model can be certain about a misread. An 8-digit Omang can't be
    # right, whatever confidence was reported - the score must drop so a
    # human looks at it.
    fields = {
        "id_type": FieldValue(field_id="id_type", value="Omang", confidence=95.0),
        "id_number": FieldValue(field_id="id_number", value="12345678", confidence=95.0),
    }
    malformed = apply_field_validation("APPFORM", fields)
    assert "id_number" in malformed
    assert fields["id_number"].confidence < 95.0
