"""
Module 3: Validation Engine (Section 4.2 / 7.2 of the alignment document).

Deliberately 100% deterministic Python - no LLM calls. Validation rules are
factual/regulatory (ID digit counts, date logic, percentage totals) and gain
nothing from an LLM, so keeping this rule-based:
  - guarantees the "100% rule coverage" success criterion (Section 9) is met
    reliably, not probabilistically
  - costs zero extra Ollama calls per form
  - lets Operations edit config/validation_rules.json to add a new rule
    without touching code, satisfying "no code changes" requirement (7.3)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.models.schemas import (
    ExtractionResult,
    FieldValidationResult,
    ValidationReport,
    ValidationStatus,
)
from app.utils.config_loader import load_validation_rules
from app.utils.logger import get_logger

logger = get_logger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"]


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _nth_digit(number_str: str, n: int) -> str | None:
    digits = re.sub(r"\D", "", number_str or "")
    if len(digits) < n:
        return None
    return digits[n - 1]


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


# ---------------------------------------------------------------------------
# Individual rule evaluators - one function per "logic.type" in validation_rules.json
# ---------------------------------------------------------------------------

def _eval_exact_digits(value: Any, length: int, only_if_present: bool = False) -> FieldValidationResult | None:
    if only_if_present and _is_blank(value):
        return None
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == length and digits == str(value or "").strip():
        return FieldValidationResult("", value, ValidationStatus.PASS)
    if len(digits) == length:
        return FieldValidationResult("", value, ValidationStatus.PASS)
    return FieldValidationResult("", value, ValidationStatus.FAIL, f"Expected exactly {length} digits, got '{value}'")


def _eval_id_number_format(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    id_type = extraction.field_value("id_type")
    id_number = extraction.field_value("id_number")
    logic = rule["logic"]

    if id_type not in logic:
        return FieldValidationResult("id_number", id_number, ValidationStatus.WARNING, f"Unknown ID type '{id_type}' - cannot apply digit validation")

    sub = logic[id_type]
    if sub["type"] == "skip":
        status = ValidationStatus(sub.get("status_if_present", "WARNING")) if not _is_blank(id_number) else ValidationStatus.WARNING
        return FieldValidationResult("id_number", id_number, status, sub.get("message", ""))

    if sub["type"] == "exact_digits":
        result = _eval_exact_digits(id_number, sub["length"])
        result.field_id = "id_number"
        return result

    return FieldValidationResult("id_number", id_number, ValidationStatus.WARNING, "No applicable rule")


def _eval_gender_derivation(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    logic = rule["logic"]
    id_type = extraction.field_value("id_type")
    id_number = extraction.field_value(logic["source_field"])

    if id_type not in logic["applicable_id_types"]:
        return FieldValidationResult("gender", extraction.field_value("gender"), ValidationStatus.PASS,
                                      "Gender derivation not applicable for this ID type")

    digit = _nth_digit(str(id_number or ""), logic["digit_index"])
    derived = logic["mapping"].get(digit) if digit else None

    if derived is None:
        return FieldValidationResult("gender", None, ValidationStatus.WARNING,
                                      f"Could not derive gender - 5th digit of ID number unreadable ('{id_number}')")

    stated = extraction.field_value("gender")
    if stated and stated != derived:
        return FieldValidationResult("gender", derived, ValidationStatus.WARNING,
                                      f"Derived gender '{derived}' differs from captured value '{stated}'")
    return FieldValidationResult("gender", derived, ValidationStatus.PASS)


def _eval_id_expiry_future(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    logic = rule["logic"]
    skip_if = logic.get("skip_if", {})
    if skip_if and extraction.field_value(skip_if["field"]) == skip_if["equals"]:
        return FieldValidationResult("id_expiry_date", extraction.field_value("id_expiry_date"), ValidationStatus.PASS,
                                      "Skipped - Birth Certificates do not expire")

    raw_value = extraction.field_value("id_expiry_date")
    parsed = _parse_date(raw_value)
    if parsed is None:
        if _is_blank(raw_value):
            return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.WARNING, "ID expiry date not captured")
        return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.FAIL, f"Unparseable date: '{raw_value}'")

    if parsed <= datetime.now():
        return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.FAIL, "ID expiry date is not in the future")
    return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.PASS)


def _eval_date_of_birth_past(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    raw_value = extraction.field_value("date_of_birth")
    parsed = _parse_date(raw_value)
    if parsed is None:
        return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.FAIL, f"Unparseable or missing date of birth: '{raw_value}'")
    if parsed >= datetime.now():
        return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.FAIL, "Date of birth must be in the past")
    return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.PASS)


def _eval_regex(extraction: ExtractionResult, field_id: str, rule: dict) -> FieldValidationResult:
    value = extraction.field_value(field_id)
    if _is_blank(value):
        return FieldValidationResult(field_id, value, ValidationStatus.FAIL, "Required field is blank")
    pattern = rule["logic"]["pattern"]
    if re.match(pattern, str(value)):
        return FieldValidationResult(field_id, value, ValidationStatus.PASS)
    return FieldValidationResult(field_id, value, ValidationStatus.FAIL, f"'{value}' does not match expected format")


def _eval_numeric_length(extraction: ExtractionResult, field_id: str, rule: dict) -> FieldValidationResult:
    value = extraction.field_value(field_id)
    logic = rule["logic"]
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits.isdigit():
        return FieldValidationResult(field_id, value, ValidationStatus.FAIL, "Not numeric")
    status = ValidationStatus.PASS if len(digits) == logic["expected_length"] else ValidationStatus(logic["status_if_mismatch"])
    msg = "" if status == ValidationStatus.PASS else f"Expected {logic['expected_length']} digits, got {len(digits)}"
    return FieldValidationResult(field_id, value, status, msg)


def _eval_conditional_required(extraction: ExtractionResult, rule: dict) -> list[FieldValidationResult]:
    logic = rule["logic"]
    condition_met = extraction.field_value(logic["condition_field"]) == logic["condition_equals"]
    results = []
    for fid in logic["required_fields"]:
        value = extraction.field_value(fid)
        if condition_met and _is_blank(value):
            results.append(FieldValidationResult(fid, value, ValidationStatus.FAIL,
                                                   f"Required because {logic['condition_field']} = '{logic['condition_equals']}'"))
        else:
            results.append(FieldValidationResult(fid, value, ValidationStatus.PASS))
    return results


def _eval_default_if_blank(extraction: ExtractionResult, field_id: str, rule: dict) -> FieldValidationResult:
    value = extraction.field_value(field_id)
    default = rule["logic"]["default"]
    if _is_blank(value):
        return FieldValidationResult(field_id, default, ValidationStatus.PASS, f"Defaulted to '{default}' (blank on form)")
    return FieldValidationResult(field_id, value, ValidationStatus.PASS)


def _eval_beneficiary_split_total(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    if not extraction.beneficiaries:
        return FieldValidationResult("beneficiaries", None, ValidationStatus.WARNING, "No beneficiaries captured")
    total = sum(b.split_percent for b in extraction.beneficiaries)
    target = rule["logic"]["target"]
    if abs(total - target) < 0.01:
        return FieldValidationResult("beneficiaries", total, ValidationStatus.PASS)
    return FieldValidationResult("beneficiaries", total, ValidationStatus.FAIL,
                                  f"Beneficiary split totals {total}%, expected {target}%")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_RULE_DISPATCH = {
    "id_number_format": lambda ex, rule: [_eval_id_number_format(ex, rule)],
    "gender_derivation": lambda ex, rule: [_eval_gender_derivation(ex, rule)],
    "id_expiry_future": lambda ex, rule: [_eval_id_expiry_future(ex, rule)],
    "date_of_birth_past": lambda ex, rule: [_eval_date_of_birth_past(ex, rule)],
    "email_format": lambda ex, rule: [_eval_regex(ex, "email", rule)],
    "contact_number_numeric": lambda ex, rule: [_eval_numeric_length(ex, "contact_number", rule)],
    "minor_fields_required_if_acting_on_behalf": lambda ex, rule: _eval_conditional_required(ex, rule),
    "guardian_id_format": lambda ex, rule: [
        r for r in [_eval_exact_digits(ex.field_value("guardian_id_number"), rule["logic"]["length"], rule["logic"].get("only_if_present", False))]
        if r is not None
    ],
    "pref_sms_default": lambda ex, rule: [_eval_default_if_blank(ex, "pref_sms", rule)],
    "pref_marketing_default": lambda ex, rule: [_eval_default_if_blank(ex, "pref_marketing", rule)],
    "communication_channel_default": lambda ex, rule: [_eval_default_if_blank(ex, "communication_channel", rule)],
    "beneficiary_split_total": lambda ex, rule: [_eval_beneficiary_split_total(ex, rule)],
}


def validate(extraction: ExtractionResult) -> ValidationReport:
    """Applies every rule in config/validation_rules.json to an ExtractionResult."""
    rules_config = load_validation_rules()
    results: list[FieldValidationResult] = []

    # 1. Mandatory field presence check (independent of rule-specific logic)
    for field_id in rules_config.get("mandatory_fields", []):
        value = extraction.field_value(field_id)
        if _is_blank(value):
            results.append(FieldValidationResult(field_id, value, ValidationStatus.FAIL, "Mandatory field missing"))

    # 2. Rule-specific evaluation
    for rule in rules_config.get("rules", []):
        handler = _RULE_DISPATCH.get(rule["id"])
        if handler is None:
            logger.warning("No handler implemented for rule '%s' - skipping", rule["id"])
            continue
        for fr in handler(extraction, rule):
            if fr.field_id == "" and "applies_to" in rule:
                fr.field_id = rule["applies_to"]
            results.append(fr)

    entity_number = str(extraction.field_value("id_number") or extraction.source_file)
    report = ValidationReport(source_file=extraction.source_file, entity_number=entity_number, results=results)
    logger.info("Validated %s -> overall status %s (%d checks)", extraction.source_file, report.overall_status.value, len(results))
    return report
