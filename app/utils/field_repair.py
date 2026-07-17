from __future__ import annotations

import difflib
import re

from app.utils.config_loader import load_form_types

# Repair outcomes, returned alongside the (possibly repaired) value.
OK = "ok"                      # already satisfied its constraint
REPAIRED = "repaired"          # violated it, but was unambiguously fixable
UNREPAIRABLE = "unrepairable"  # violates it and could not be fixed safely
NOT_APPLICABLE = "n/a"         # no known constraint for this field

# Classic OCR / handwriting look-alikes, letter -> digit. Only used on
# fields that are known to be all-digits, and only when the substitution
# lands the value on its exact required length (see _repair_fixed_digits),
# so a genuinely wrong read can't be mangled into a wrong-but-valid one.
_OCR_LETTER_TO_DIGIT = {
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "Z": "2", "z": "2",
    "E": "3",
    "A": "4",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
}

# Fields that are a fixed number of digits regardless of anything else.
# id_number is NOT here: it's 9 digits only for an Omang / Birth
# Certificate, while a passport number's format varies by country of
# issue - see _expected_digit_length.
_FIXED_DIGIT_FIELDS = {
    "branch_code": 6,
    "guardian_id_number": 9,
}

# id_number is 9 digits only for these ID types (matches the
# id_number_format rule in config/validation_rules.json).
_NINE_DIGIT_ID_TYPES = {"omang", "birth certificate"}

# Separators a human or the model may write inside a long number.
_SEPARATORS_RE = re.compile(r"[\s\-/.,]")


def _expected_digit_length(field_id: str, all_fields: dict) -> int | None:
    """The exact digit length this field must have, or None if it has no
    fixed-length constraint (or we can't establish one for this record)."""
    if field_id in _FIXED_DIGIT_FIELDS:
        return _FIXED_DIGIT_FIELDS[field_id]
    if field_id == "id_number":
        id_type = all_fields.get("id_type")
        id_type_value = getattr(id_type, "value", id_type)
        if str(id_type_value or "").strip().casefold() in _NINE_DIGIT_ID_TYPES:
            return 9
    return None


def _repair_fixed_digits(value, length: int) -> tuple[str, str]:
    """Returns (value, outcome) for a field that must be exactly `length`
    digits. Never pads or truncates - a value that is the wrong length for
    any reason other than pure letter/digit confusion is left alone."""
    text = _SEPARATORS_RE.sub("", str(value))
    if re.fullmatch(rf"\d{{{length}}}", text):
        return text, OK

    # Only attempt a repair when every character is either already a digit
    # or a known look-alike. Anything else (a real letter, punctuation) is
    # a sign this isn't a simple misread, so don't touch it.
    if not text or not all(c.isdigit() or c in _OCR_LETTER_TO_DIGIT for c in text):
        return str(value), UNREPAIRABLE

    repaired = "".join(_OCR_LETTER_TO_DIGIT.get(c, c) for c in text)
    if re.fullmatch(rf"\d{{{length}}}", repaired):
        return repaired, REPAIRED
    return str(value), UNREPAIRABLE


def _snap_to_candidates(value, candidates: list[str], cutoff: float) -> tuple[str, str]:
    """Snaps a value onto exactly one of `candidates`, or leaves it alone.

    Tries, in order: exact match, case-insensitive match, unambiguous
    substring containment (the model often returns 'Salaried' for
    'Salaried employee'), then close fuzzy match. Any step that yields
    more than one candidate is treated as ambiguous and skipped rather
    than guessed at.
    """
    text = str(value).strip()
    if not text or not candidates:
        return value, NOT_APPLICABLE

    for candidate in candidates:
        if text == candidate:
            return candidate, OK
    folded = text.casefold()
    for candidate in candidates:
        if folded == candidate.casefold():
            return candidate, REPAIRED

    contained = [
        c for c in candidates
        if folded and (folded in c.casefold() or c.casefold() in folded)
    ]
    if len(contained) == 1:
        return contained[0], REPAIRED

    close = difflib.get_close_matches(text, candidates, n=2, cutoff=cutoff)
    if len(close) == 1:
        return close[0], REPAIRED

    return value, UNREPAIRABLE


def _known_fund_names() -> list[str]:
    return [f["name"] for f in load_form_types().get("funds", []) if f.get("name")]


def repair_field(field_id: str, value, field_def: dict, all_fields: dict) -> tuple[object, str]:
    """
    Returns (value, outcome) - the value repaired onto its known domain
    where that's provably safe, otherwise unchanged.

    Runs BEFORE the shape normalisation in app.utils.field_validators,
    because that step strips non-digits out of ID-shaped fields: it would
    turn "12345O789" into an 8-digit "12345789" and destroy the very
    evidence this module needs to recognise the O/0 confusion.
    """
    if value is None or str(value).strip() == "":
        return value, NOT_APPLICABLE

    length = _expected_digit_length(field_id, all_fields)
    if length is not None:
        return _repair_fixed_digits(value, length)

    if field_id in ("fund_name",):
        # Strip the '*'/'**' lock-in markers printed next to fund names on
        # the form before matching.
        cleaned = re.sub(r"^\**\s*", "", str(value)).strip()
        return _snap_to_candidates(cleaned, _known_fund_names(), cutoff=0.7)

    options = field_def.get("options")
    if options:
        return _snap_to_candidates(value, [str(o) for o in options], cutoff=0.8)

    return value, NOT_APPLICABLE
