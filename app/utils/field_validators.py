"""
Post-extraction field validation & normalization.

Vision-model confidence scores reflect the model's own certainty about
what it read, not whether the value is actually well-formed. This module
is a second, independent check: for fields with a known expected shape
(email, phone, numeric IDs, percentages, currency), validate the
extracted value against that shape and adjust confidence accordingly -
up a little when the value is well-formed and needed no correction, up
more when a safe, unambiguous correction was applied (e.g. a well-known
email domain typo), and DOWN when the value doesn't match its expected
shape at all, regardless of what confidence the model itself reported.
A model can be "confident" about a misread digit; this catches what the
model can't catch about itself.

Deliberately conservative: it normalizes formatting (whitespace, casing,
stray symbols) and fixes a short list of well-known typos, but it never
invents or guesses at content it can't verify - e.g. a negative currency
amount is flagged for review, not silently flipped positive.

Runs after multi-pass extraction/merging (app.services.extractor), before
derived-metadata computation, since some derived fields (fund_category)
depend on a clean fund_name.
"""
from __future__ import annotations

import re
from typing import Callable

from app.models.schemas import FieldValue
from app.utils.confidence import (
    DOMAIN_REPAIRED_BONUS,
    DOMAIN_VIOLATION_PENALTY,
    cap_confidence,
)
from app.utils import field_repair
from app.utils.config_loader import get_fields_for_form
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Small, safe nudges only - this is a confirmation layer on top of the
# model's own read, not a replacement for it.
WELL_FORMED_BONUS = 4.0
CORRECTED_BONUS = 6.0
MALFORMED_PENALTY = 25.0

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Common OCR/handwriting misreads of well-known email domains. Corrected
# only when the local part + the rest of the domain structure already
# looks like an email - this fixes the domain, it never invents one.
_EMAIL_DOMAIN_FIXES = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gnail.com": "gmail.com", "gmall.com": "gmail.com",
    "yahou.com": "yahoo.com", "yaho.com": "yahoo.com", "yahoo.co": "yahoo.com",
    "yahoo.con": "yahoo.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com",
    "hotmai.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "outlok.com": "outlook.com", "outllok.com": "outlook.com",
    "outlook.co": "outlook.com",
}

_PHONE_DIGITS_MIN = 7
_PHONE_DIGITS_MAX = 15


def _normalize_email(value) -> tuple[str, bool, bool]:
    """Returns (normalized_value, is_well_formed, was_corrected).
    Strips stray whitespace handwriting extraction sometimes inserts
    around '@' and '.' (an email never legitimately contains a space),
    lowercases, and fixes known domain typos."""
    original = str(value).strip()
    cleaned = re.sub(r"\s+", "", original).lower()
    if "@" in cleaned:
        local, _, domain = cleaned.rpartition("@")
        if domain in _EMAIL_DOMAIN_FIXES:
            domain = _EMAIL_DOMAIN_FIXES[domain]
        cleaned = f"{local}@{domain}"
    is_well_formed = bool(_EMAIL_RE.match(cleaned))
    corrected = cleaned != original
    return cleaned, is_well_formed, corrected


def _normalize_phone(value) -> tuple[str, bool, bool]:
    original = str(value).strip()
    digits = re.sub(r"[^\d+]", "", original)
    digit_count = len(re.sub(r"\D", "", digits))
    is_well_formed = _PHONE_DIGITS_MIN <= digit_count <= _PHONE_DIGITS_MAX
    corrected = digits != original
    return digits, is_well_formed, corrected


def _normalize_numeric_id(value) -> tuple[str, bool, bool]:
    """entity_number / fund_number / account_number / branch_code /
    id_number - strip everything but digits, preserving the exact digit
    string (no padding/truncation - lengths vary legitimately by field)."""
    original = str(value).strip()
    digits = re.sub(r"\D", "", original)
    is_well_formed = len(digits) > 0
    corrected = digits != original
    return digits, is_well_formed, corrected


def _normalize_percentage(value) -> tuple[str, bool, bool]:
    original = str(value).strip()
    cleaned = re.sub(r"[^\d.]", "", original)
    try:
        num = float(cleaned) if cleaned else None
    except ValueError:
        num = None
    is_well_formed = num is not None and 0 <= num <= 100
    corrected = cleaned != original
    return (cleaned or original), is_well_formed, corrected


def _normalize_currency(value) -> tuple[str, bool, bool]:
    """Strips currency symbols/thousand-separators but keeps a leading
    '-' if present - a negative amount is flagged as malformed (for human
    review) rather than silently made positive, since that's exactly the
    kind of silent 'correction' that would hide a real data problem on a
    money field."""
    original = str(value).strip()
    cleaned = re.sub(r"[^\d.\-]", "", original)
    try:
        parsed = float(cleaned) if cleaned not in ("", "-") else None
    except ValueError:
        parsed = None
    is_well_formed = parsed is not None and parsed >= 0
    corrected = cleaned != original
    return (cleaned if cleaned else original), is_well_formed, corrected


def _normalize_text(value) -> tuple[str, bool, bool]:
    original = str(value)
    cleaned = re.sub(r"\s+", " ", original).strip()
    is_well_formed = len(cleaned) > 0
    corrected = cleaned != original
    return cleaned, is_well_formed, corrected


# Field-type (from field_definitions.json's "type") -> normalizer.
# Fields not listed here (checkbox, select, plain text) get whitespace
# cleanup only via _normalize_text - a select/checkbox's correctness is
# about matching an allowed option, which validation_rules.json's
# mandatory-field/allowed-values checks already cover in
# app.validation.engine, not this module.
_VALIDATORS: dict[str, Callable] = {
    "email": _normalize_email,
    "phone": _normalize_phone,
    "percentage": _normalize_percentage,
    "currency": _normalize_currency,
}

# Field IDs that are always numeric-ID shaped regardless of their
# declared "type" in field_definitions.json (they're commonly typed as
# "text" there since they're transcribed as strings, not arithmetic
# numbers).
_NUMERIC_ID_FIELDS = {
    "entity_number", "fund_number", "account_number", "branch_code", "id_number",
}


def _validator_for(field_id: str, declared_type: str) -> Callable:
    if field_id in _NUMERIC_ID_FIELDS:
        return _normalize_numeric_id
    return _VALIDATORS.get(declared_type, _normalize_text)


def apply_field_validation(form_code: str, fields: dict[str, FieldValue]) -> list[str]:
    """
    In-place: normalizes and confidence-adjusts every field in `fields`
    against its declared type. Returns the list of field_ids that failed
    validation (malformed shape) for logging/audit - these are exactly
    the fields most worth a human glancing at, regardless of what
    confidence the model itself reported.

    NOTE: assumes field_definitions.json uses "email" / "phone" /
    "percentage" / "currency" as the "type" values for those field
    shapes. If your config uses different type strings, update
    _VALIDATORS' keys to match - anything not matched falls back to
    harmless whitespace cleanup only, it doesn't error.
    """
    field_defs = {f["id"]: f for f in get_fields_for_form(form_code)}
    malformed: list[str] = []
    repaired: list[str] = []

    for field_id, fv in fields.items():
        if fv.value is None or str(fv.value).strip() == "":
            continue
        declared_type = field_defs.get(field_id, {}).get("type", "text")
        validator = _validator_for(field_id, declared_type)

        # 1. Domain-constrained repair FIRST, while the raw model output is
        #    still intact - the numeric-ID normalizer below strips letters
        #    out, which would destroy the O/0-style evidence field_repair
        #    needs. See app.utils.field_repair's module docstring.
        try:
            fv.value, outcome = field_repair.repair_field(
                field_id, fv.value, field_defs.get(field_id, {}), fields,
            )
        except Exception:  # noqa: BLE001 - repair must never crash extraction
            logger.warning("Domain repair failed for field '%s' on form %s", field_id, form_code)
            outcome = field_repair.NOT_APPLICABLE

        if outcome == field_repair.REPAIRED:
            # Now provably a legal value for this field, but the raw read
            # was wrong - worth a small nudge up, not a clean bill of health.
            fv.confidence = cap_confidence(fv.confidence + DOMAIN_REPAIRED_BONUS)
            repaired.append(field_id)
        elif outcome == field_repair.UNREPAIRABLE:
            # Violates a constraint we know for certain and couldn't be
            # fixed safely - exactly the field a human should look at,
            # whatever confidence the model itself reported.
            fv.confidence = max(0.0, fv.confidence - DOMAIN_VIOLATION_PENALTY)
            malformed.append(field_id)

        # 2. Shape normalization / confidence calibration, as before.
        try:
            normalized, is_well_formed, was_corrected = validator(fv.value)
        except Exception:  # noqa: BLE001 - a validator must never crash extraction
            logger.warning("Validator failed for field '%s' on form %s", field_id, form_code)
            continue

        fv.value = normalized
        if not is_well_formed:
            fv.confidence = max(0.0, fv.confidence - MALFORMED_PENALTY)
            if field_id not in malformed:
                malformed.append(field_id)
        elif was_corrected:
            fv.confidence = cap_confidence(fv.confidence + CORRECTED_BONUS)
        else:
            fv.confidence = cap_confidence(fv.confidence + WELL_FORMED_BONUS)

    if repaired:
        logger.info(
            "Form %s: %d field(s) repaired onto their known domain: %s",
            form_code, len(repaired), ", ".join(repaired),
        )

    if malformed:
        logger.info(
            "Form %s: %d field(s) failed shape validation despite model confidence: %s",
            form_code, len(malformed), ", ".join(malformed),
        )
    return malformed