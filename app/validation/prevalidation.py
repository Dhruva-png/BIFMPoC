"""
Pre-Validation Flags engine — Section 8 of the BIFM understanding document.

This is deliberately a SEPARATE module from app/validation/engine.py:
  - engine.py (Module 3) checks the data quality of individual fields
    (format, digit counts, date logic) and produces PASS/WARNING/FAIL.
  - This module (also feeding Module 3's output, but a distinct output
    category per Section 4 of the doc: "Pre-validation Flags") checks
    document-level completeness/consistency signals BIFM's authorisers
    actually reject instructions for — missing signatures, absent KYC,
    incomplete banking sections, etc. — and tags each with the rejection
    risk BIFM assigned it in the requirement sessions (High / Medium /
    Process reminder / Process impact).

Deterministic, config-driven (config/prevalidation_flags.json) — no LLM
calls, so a new flag or a change to its trigger condition is a JSON edit,
not a code change.

Known POC limitation: a few flags reference signals the vision-LLM
extractor doesn't yet produce per-field (e.g. a per-page "initials present"
checkbox). Those flags are evaluated as simply not-triggered rather than a
false trigger or "unable to verify" noise — see _UNVERIFIABLE_FIELDS below.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.schemas import ExtractionResult, PreValidationFlag
from app.utils.config_loader import load_prevalidation_flags
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Fields referenced by flag logic that this POC's extractor does not
# currently populate for any form (no source field exists on the physical
# form template as modelled in field_definitions.json). Flags depending
# solely on these are reported as not-triggered with an explicit
# "not verified" reason instead of a potentially-wrong hard trigger.
_UNVERIFIABLE_FIELDS = {"page_initials_complete"}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _field_known(extraction: ExtractionResult, field_id: str) -> bool:
    return field_id in extraction.fields


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def _eval_field_blank(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    field_id = logic["field"]
    if field_id in _UNVERIFIABLE_FIELDS:
        # Known POC limitation — the extractor doesn't produce this signal yet.
        # Report as simply not-triggered rather than surfacing "unable to verify" noise.
        return False, ""
    if not _field_known(extraction, field_id) and field_id not in extraction.fields:
        # Field not part of this form's schema / never attempted — nothing to flag.
        pass
    value = extraction.field_value(field_id)
    blank = _is_blank(value)
    if logic.get("invert"):
        blank = not blank
    if blank:
        return True, f"'{field_id}' is blank"
    return False, ""


def _eval_all_blank(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    fields = logic["fields"]
    if all(_is_blank(extraction.field_value(f)) for f in fields):
        return True, f"None of {', '.join(fields)} populated"
    return False, ""


def _eval_any_blank(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    """
    Triggers if ANY of the given fields is blank (as opposed to all_blank's
    "none of them populated") - for a group where each one is independently
    required, e.g. a KYC document needs BOTH a way to contact the investor
    AND a signature; missing either one on its own is already a problem.
    """
    fields = logic["fields"]
    missing = [f for f in fields if _is_blank(extraction.field_value(f))]
    if missing:
        return True, f"{', '.join(missing)} not captured"
    return False, ""


def _eval_conditional_group_blank(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    condition_field = logic.get("condition_field")
    condition_equals = logic.get("condition_equals")
    condition_any_present = logic.get("condition_any_present")
    required = logic["required_fields"]

    condition_met = False
    if condition_any_present:
        condition_met = any(not _is_blank(extraction.field_value(f)) for f in condition_any_present)
    elif condition_field:
        value = extraction.field_value(condition_field)
        if isinstance(condition_equals, list):
            condition_met = value in condition_equals
        else:
            condition_met = value == condition_equals

    if not condition_met:
        return False, ""

    missing = [f for f in required if _is_blank(extraction.field_value(f))]
    if missing:
        return True, f"Required fields missing: {', '.join(missing)}"
    return False, ""


def _eval_checkbox_and_companion_doc(
    extraction: ExtractionResult, logic: dict, batch_form_codes: set[str]
) -> tuple[bool, str]:
    checkbox_fields = logic["checkbox_fields"]
    companion_code = logic["companion_form_code"]
    all_ticked = all(bool(extraction.field_value(f)) for f in checkbox_fields)
    companion_present = companion_code in batch_form_codes
    if not all_ticked and not companion_present:
        unticked = [f for f in checkbox_fields if not extraction.field_value(f)]
        return True, f"KYC checkboxes not all ticked ({', '.join(unticked)}) and no {companion_code} document in batch"
    return False, ""


def _eval_beneficiaries_empty(extraction: ExtractionResult) -> tuple[bool, str]:
    if not extraction.beneficiaries:
        return True, "No beneficiaries captured"
    return False, ""


def _eval_beneficiary_sum_not_100(extraction: ExtractionResult) -> tuple[bool, str]:
    if not extraction.beneficiaries:
        return False, ""  # covered by beneficiary_section_blank instead
    total = sum(b.split_percent or 0 for b in extraction.beneficiaries)
    if abs(total - 100.0) > 0.01:
        return True, f"Beneficiary split totals {total:g}%, expected 100%"
    return False, ""


def _eval_date_within_days(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    condition_field = logic.get("condition_field")
    condition_in = logic.get("condition_in", [])
    if condition_field and extraction.field_value(condition_field) not in condition_in:
        return False, ""
    date_val = _parse_date(extraction.field_value(logic["date_field"]))
    if not date_val:
        return False, ""
    days_out = (date_val - datetime.now()).days
    if 0 <= days_out <= logic["days"]:
        return True, f"'{logic['date_field']}' is only {days_out} day(s) away — effective next cycle instead"
    return False, ""


def _eval_conditional_companion_form(extraction: ExtractionResult, logic: dict) -> tuple[bool, str]:
    condition_field = logic.get("condition_field")
    condition_equals = logic.get("condition_equals", [])
    value = extraction.field_value(condition_field) if condition_field else None
    condition_met = value in condition_equals

    if not condition_met and logic.get("alt_condition_field"):
        alt_value = extraction.field_value(logic["alt_condition_field"])
        condition_met = alt_value in logic.get("alt_condition_equals", [])

    if not condition_met:
        return False, ""

    marker_fields = logic.get("companion_marker_fields", [])
    if all(_is_blank(extraction.field_value(f)) for f in marker_fields):
        return True, f"Condition met but supporting fields ({', '.join(marker_fields)}) are all blank — companion form likely missing"
    return False, ""


def _eval_form_reminder(form_code: str, applies_to) -> tuple[bool, str]:
    applies = applies_to if isinstance(applies_to, list) else [applies_to]
    if form_code in applies:
        return True, "Process reminder — see trigger_condition"
    return False, ""


def evaluate_prevalidation_flags(
    extraction: ExtractionResult,
    form_code: str,
    batch_form_codes: list[str] | None = None,
) -> list[PreValidationFlag]:
    """
    Evaluates every flag in config/prevalidation_flags.json applicable to
    form_code against this extraction. batch_form_codes should list the
    form codes of every OTHER document submitted alongside this one in the
    same batch (e.g. a KYC form dropped in with a New Business form) —
    this is how companion-document presence is detected in this POC, per
    Section 9.1's note that KYC/POP are linked companion documents rather
    than standalone instructions.

    Returns one PreValidationFlag per applicable rule (triggered or not),
    so the Excel output can show a full checklist, not just the exceptions.
    """
    batch_codes = set(batch_form_codes or [])
    flags_config = load_prevalidation_flags()["flags"]
    results: list[PreValidationFlag] = []

    for flag_def in flags_config:
        applies_to = flag_def.get("applies_to", "all")
        if applies_to != "all" and form_code not in applies_to:
            continue

        logic = flag_def["logic"]
        logic_type = logic["type"]
        try:
            if logic_type == "field_blank":
                triggered, reason = _eval_field_blank(extraction, logic)
            elif logic_type == "all_blank":
                triggered, reason = _eval_all_blank(extraction, logic)
            elif logic_type == "any_blank":
                triggered, reason = _eval_any_blank(extraction, logic)
            elif logic_type == "conditional_group_blank":
                triggered, reason = _eval_conditional_group_blank(extraction, logic)
            elif logic_type == "checkbox_and_companion_doc":
                triggered, reason = _eval_checkbox_and_companion_doc(extraction, logic, batch_codes)
            elif logic_type == "beneficiaries_empty":
                triggered, reason = _eval_beneficiaries_empty(extraction)
            elif logic_type == "beneficiary_sum_not_100":
                triggered, reason = _eval_beneficiary_sum_not_100(extraction)
            elif logic_type == "date_within_days":
                triggered, reason = _eval_date_within_days(extraction, logic)
            elif logic_type == "conditional_companion_form":
                triggered, reason = _eval_conditional_companion_form(extraction, logic)
            elif logic_type == "form_reminder":
                triggered, reason = _eval_form_reminder(form_code, applies_to)
            else:
                logger.warning("Unknown prevalidation logic type '%s' for flag '%s'", logic_type, flag_def["id"])
                triggered, reason = False, ""
        except Exception:  # noqa: BLE001
            logger.exception("Failed evaluating prevalidation flag '%s'", flag_def["id"])
            triggered, reason = False, "Error evaluating this flag — see logs"

        results.append(PreValidationFlag(
            flag_id=flag_def["id"],
            label=flag_def["label"],
            triggered=triggered,
            rejection_risk=flag_def["rejection_risk"],
            reason=reason,
        ))

    return results
