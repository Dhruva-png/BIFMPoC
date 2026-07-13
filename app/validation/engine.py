"""
Module 3: Validation Engine — supports ALL 6 BIFM UT form types.

Deliberately 100% deterministic Python — no LLM calls. Validation rules are
factual/regulatory (ID digit counts, date logic, percentage totals) and gain
nothing from an LLM. This guarantees reliable, auditable, zero-cost validation.

Validation is form-aware: mandatory fields and applicable rules differ by form
type. The engine reads mandatory_fields from field_definitions.json per form
code, then applies cross-cutting rules from validation_rules.json where the
relevant fields are present in the extraction.
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
from app.utils.config_loader import (
    get_mandatory_fields_for_form,
    load_validation_rules,
)
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
# Rule evaluators
# ---------------------------------------------------------------------------

def _eval_exact_digits(value: Any, length: int, only_if_present: bool = False) -> FieldValidationResult | None:
    if only_if_present and _is_blank(value):
        return None
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == length:
        return FieldValidationResult("", value, ValidationStatus.PASS)
    return FieldValidationResult("", value, ValidationStatus.FAIL, f"Expected exactly {length} digits, got '{value}'")


def _eval_id_number_format(extraction: ExtractionResult, rule: dict) -> FieldValidationResult | None:
    id_type = extraction.field_value("id_type")
    id_number = extraction.field_value("id_number")

    if _is_blank(id_number):
        # id_number is optional on some forms (DEBIT, DIS, STATIC, KYC track by entity).
        # Mandatory-field checking already flags this where it matters — no need for a
        # second "couldn't verify" notice here.
        return None

    logic = rule["logic"]
    if id_type not in logic:
        # Unrecognised ID type on the form — nothing meaningful to check against.
        return None

    sub = logic[id_type]
    if sub["type"] == "skip":
        status = ValidationStatus(sub.get("status_if_present", "WARNING")) if not _is_blank(id_number) else ValidationStatus.WARNING
        return FieldValidationResult("id_number", id_number, status, sub.get("message", ""))
    if sub["type"] == "exact_digits":
        result = _eval_exact_digits(id_number, sub["length"])
        if result:
            result.field_id = "id_number"
        return result

    return None


def _eval_gender_derivation(extraction: ExtractionResult, rule: dict) -> FieldValidationResult | None:
    logic = rule["logic"]
    id_type = extraction.field_value("id_type")
    id_number = extraction.field_value(logic["source_field"])

    if id_type not in logic["applicable_id_types"]:
        return FieldValidationResult("gender", extraction.field_value("gender"), ValidationStatus.PASS,
                                      "Gender derivation not applicable for this ID type")

    digit = _nth_digit(str(id_number or ""), logic["digit_index"])
    derived = logic["mapping"].get(digit) if digit else None

    if derived is None:
        # ID number missing/unreadable — nothing to cross-check gender against.
        return None

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
    if _is_blank(raw_value):
        return None

    parsed = _parse_date(raw_value)
    if parsed is None:
        return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.FAIL, f"Unparseable date: '{raw_value}'")
    if parsed <= datetime.now():
        return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.FAIL, "ID expiry date is not in the future")
    return FieldValidationResult("id_expiry_date", raw_value, ValidationStatus.PASS)


def _eval_date_of_birth_past(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    raw_value = extraction.field_value("date_of_birth")
    if _is_blank(raw_value):
        # DOB is mandatory for APPFORM/KYC but not all forms; mandatory-field
        # checking already flags it where required, so skip the extra notice.
        return None
    parsed = _parse_date(raw_value)
    if parsed is None:
        return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.FAIL, f"Unparseable date: '{raw_value}'")
    if parsed >= datetime.now():
        return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.FAIL, "Date of birth must be in the past")
    return FieldValidationResult("date_of_birth", raw_value, ValidationStatus.PASS)


def _eval_regex(extraction: ExtractionResult, field_id: str, rule: dict) -> FieldValidationResult | None:
    value = extraction.field_value(field_id)
    if _is_blank(value):
        return None
    pattern = rule["logic"]["pattern"]
    if re.match(pattern, str(value)):
        return FieldValidationResult(field_id, value, ValidationStatus.PASS)
    return FieldValidationResult(field_id, value, ValidationStatus.FAIL, f"'{value}' does not match expected format")


def _eval_numeric_length(extraction: ExtractionResult, field_id: str, rule: dict) -> FieldValidationResult | None:
    value = extraction.field_value(field_id)
    if _is_blank(value):
        return None
    logic = rule["logic"]
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return FieldValidationResult(field_id, value, ValidationStatus.FAIL, "Not numeric")
    status = ValidationStatus.PASS if len(digits) == logic["expected_length"] else ValidationStatus(logic["status_if_mismatch"])
    msg = "" if status == ValidationStatus.PASS else f"Expected {logic['expected_length']} digits, got {len(digits)}"
    return FieldValidationResult(field_id, value, status, msg)


def _eval_branch_code_format(extraction: ExtractionResult, rule: dict) -> FieldValidationResult | None:
    """Branch codes must be exactly 6 digits per BIFM requirements."""
    value = extraction.field_value("branch_code")
    if _is_blank(value):
        return None
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 6:
        return FieldValidationResult("branch_code", value, ValidationStatus.PASS)
    return FieldValidationResult("branch_code", value, ValidationStatus.FAIL,
                                  f"Branch code must be exactly 6 digits, got '{value}'")


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


def _eval_conditional_expected_value(extraction: ExtractionResult, rule: dict) -> FieldValidationResult | None:
    """When condition_field == condition_equals, the target field is expected
    to be one of expected_values. A mismatch is reported at the configured
    status (default WARNING) rather than a hard FAIL, since it's a
    consistency signal, not a missing mandatory field. When the condition
    isn't met, or the target field is blank (already covered by the
    mandatory-field check where relevant), nothing is reported."""
    logic = rule["logic"]
    if extraction.field_value(logic["condition_field"]) != logic["condition_equals"]:
        return None

    field_id = logic["field"]
    value = extraction.field_value(field_id)
    if _is_blank(value):
        return None

    expected = logic["expected_values"]
    expected_l = {str(e).strip().casefold() for e in expected}
    keywords = {w for e in expected for w in re.split(r"[^a-z]+", str(e).casefold()) if w}
    value_l = str(value).strip().casefold()
    if value_l in expected_l or any(k in value_l for k in keywords):
        return FieldValidationResult(field_id, value, ValidationStatus.PASS)

    status = ValidationStatus(logic.get("status_if_mismatch", "WARNING"))
    return FieldValidationResult(
        field_id, value, status,
        f"Expected one of {expected} because {logic['condition_field']} = "
        f"'{logic['condition_equals']}', got '{value}'",
    )


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


def _eval_kyc_completeness(extraction: ExtractionResult, rule: dict) -> FieldValidationResult | None:
    """KYC form: all 4 document checkboxes should be ticked."""
    flag = extraction.field_value("kyc_completeness_flag")
    if flag and "Complete" in str(flag):
        return FieldValidationResult("kyc_completeness_flag", flag, ValidationStatus.PASS)
    if flag:
        return FieldValidationResult("kyc_completeness_flag", flag, ValidationStatus.WARNING,
                                      "Not all KYC documents have been provided")
    # Flag itself wasn't computed (e.g. checkboxes unread) — nothing actionable to report.
    return None


def _eval_fund_category_present(extraction: ExtractionResult, rule: dict) -> FieldValidationResult:
    """For investment/disinvestment forms, fund category must be resolved."""
    value = extraction.field_value("fund_category")
    if _is_blank(value) or "Unknown" in str(value or ""):
        return FieldValidationResult("fund_category", value, ValidationStatus.WARNING,
                                      "Fund category could not be determined - fund name unclear")
    return FieldValidationResult("fund_category", value, ValidationStatus.PASS)


# ---------------------------------------------------------------------------
# Rule dispatch table
# ---------------------------------------------------------------------------

_RULE_DISPATCH = {
    "id_number_format": lambda ex, rule: [_eval_id_number_format(ex, rule)],
    "gender_derivation": lambda ex, rule: [_eval_gender_derivation(ex, rule)],
    "id_expiry_future": lambda ex, rule: [_eval_id_expiry_future(ex, rule)],
    "date_of_birth_past": lambda ex, rule: [_eval_date_of_birth_past(ex, rule)],
    "email_format": lambda ex, rule: [_eval_regex(ex, "email", rule)],
    "contact_number_numeric": lambda ex, rule: [_eval_numeric_length(ex, "contact_number", rule)],
    "minor_fields_required_if_acting_on_behalf": lambda ex, rule: _eval_conditional_required(ex, rule),
    "occupation_type_minor_if_acting_on_behalf": lambda ex, rule: [_eval_conditional_expected_value(ex, rule)],
    "guardian_id_format": lambda ex, rule: [
        r for r in [_eval_exact_digits(ex.field_value("guardian_id_number"), rule["logic"]["length"],
                                        rule["logic"].get("only_if_present", False))]
        if r is not None
    ],
    "pref_sms_default": lambda ex, rule: [_eval_default_if_blank(ex, "pref_sms", rule)],
    "pref_marketing_default": lambda ex, rule: [_eval_default_if_blank(ex, "pref_marketing", rule)],
    "communication_channel_default": lambda ex, rule: [_eval_default_if_blank(ex, "communication_channel", rule)],
    "beneficiary_split_total": lambda ex, rule: [_eval_beneficiary_split_total(ex, rule)],
    "branch_code_format": lambda ex, rule: [_eval_branch_code_format(ex, rule)],
    "kyc_completeness": lambda ex, rule: [_eval_kyc_completeness(ex, rule)],
    "fund_category_present": lambda ex, rule: [_eval_fund_category_present(ex, rule)],
}

# Rules that only apply to certain form types (empty set = applies to all)
_RULE_FORM_SCOPE: dict[str, set[str]] = {
    "gender_derivation": {"APPFORM", "KYC"},
    "minor_fields_required_if_acting_on_behalf": {"APPFORM"},
    "occupation_type_minor_if_acting_on_behalf": {"APPFORM"},
    "guardian_id_format": {"APPFORM"},
    "pref_sms_default": {"APPFORM"},
    "pref_marketing_default": {"APPFORM"},
    "communication_channel_default": {"APPFORM"},
    "beneficiary_split_total": {"APPFORM"},
    "date_of_birth_past": {"APPFORM", "KYC"},
    "kyc_completeness": {"KYC"},
    "fund_category_present": {"ADD", "DIS", "DIS_GSG", "DEBIT"},
    "branch_code_format": {"ADD", "DEBIT", "DIS", "DIS_GSG", "STATIC", "KYC"},
}


def _rule_applies_to_form(rule_id: str, form_code: str) -> bool:
    scope = _RULE_FORM_SCOPE.get(rule_id)
    return scope is None or len(scope) == 0 or form_code in scope


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate(extraction: ExtractionResult) -> ValidationReport:
    """
    Applies validation rules appropriate to the extracted form's type.
    Returns a ValidationReport with per-field results and overall status.
    """
    form_code = extraction.form_code
    rules_config = load_validation_rules()
    results: list[FieldValidationResult] = []

    # 1. Mandatory field presence check — driven by field_definitions.json per form type
    mandatory_fields = get_mandatory_fields_for_form(form_code)
    for field_id in mandatory_fields:
        value = extraction.field_value(field_id)
        if _is_blank(value):
            results.append(FieldValidationResult(field_id, value, ValidationStatus.FAIL, "Mandatory field missing"))

    # 2. Cross-cutting rule evaluation (only rules applicable to this form type)
    for rule in rules_config.get("rules", []):
        rule_id = rule["id"]
        if not _rule_applies_to_form(rule_id, form_code):
            continue
        handler = _RULE_DISPATCH.get(rule_id)
        if handler is None:
            logger.warning("No handler for rule '%s' - skipping", rule_id)
            continue
        for fr in handler(extraction, rule):
            if fr is None:
                continue
            if fr.field_id == "" and "applies_to" in rule:
                fr.field_id = rule["applies_to"]
            results.append(fr)

    # 3. Resolve entity_number for the report (entity_number field where present, else id_number)
    entity_number = str(
        extraction.field_value("entity_number")
        or extraction.field_value("id_number")
        or extraction.source_file
    )

    report = ValidationReport(
        source_file=extraction.source_file,
        entity_number=entity_number,
        results=results,
    )
    logger.info(
        "Validated %s (%s) → %s (%d checks)",
        extraction.source_file, form_code, report.overall_status.value, len(results),
    )
    return report
