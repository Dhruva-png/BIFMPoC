"""
Module 2: Field Extractor — supports ALL 6 BIFM UT form types.

Form types in scope:
  APPFORM  Investment Application Form
  ADD      Additional Investment Form
  DEBIT    Debit Order Form
  DIS      Disinvestment Form (Standard)
  DIS_GSG  Disinvestment Form (GSGF — Global Sustainable Growth Fund)
  STATIC   Static / Change of Investor Details Form
  KYC      KYC (Know Your Customer)

Design:
- One llava vision call per form covers the primary page(s); a second call
  is only made for APPFORM when a guardian/minor section lives on page 10.
- Each form type's field list comes from config/field_definitions.json so
  the prompt is always form-specific — not a single generic mega-prompt that
  confuses the model with fields that don't exist on the current form.
- After extraction, derived metadata fields are computed deterministically:
    fund_category, processing_cutoff, form_type_label, instruction_mode,
    kyc_completeness_flag, sub_instruction_type.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.llm.router import ask_vision, parse_json_response
from app.models.schemas import Beneficiary, ExtractionResult, FieldValue
from app.utils.confidence import CONFIDENCE_CEILING, cap_confidence
from app.utils.config_loader import (
    derive_fund_category,
    fund_category_priority,
    get_fields_for_form,
    get_mandatory_fields_for_form,
    load_field_definitions,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Page 10 of APPFORM contains the guardian/minor section
GUARDIAN_FIELD_IDS = {"guardian_name", "guardian_id_number"}
GUARDIAN_PAGE_NUMBER = 10


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

# Fund fields that live inside a "Core fund range" / fund-list table rather
# than a single dedicated form field — see _fund_table_guidance() below.
_FUND_TABLE_HINT = "fund_table"

_FUND_TABLE_GUIDANCE_COMMON = (
    "\nSPECIAL INSTRUCTIONS FOR fund_name / fund_number (fund list table):\n"
    "This form lists SEVERAL BIFM Unit Trust funds as fixed rows in a table (e.g. "
    "'Bifm Pula Money Market Fund', '* Bifm Letlotlo Education Fund', 'Bifm Balanced "
    "Prudential Fund', '* Bifm Ya Masa Junior Fund', 'Bifm Local Equity Fund', "
    "'** Bifm Global Sustainable Growth Fund' — ignore the '*'/'**' lock-in markers "
    "when extracting the name). The fund name itself is PRINTED, not handwritten. "
    "The fund number is a handwritten digit string (8 or 9 digits — capture exactly "
    "what is written, do not pad, truncate, or 'correct' the digit count) that sits in "
    "the 'Fund number' column of whichever row the client selected.\n"
    "To find the correct fund_name and fund_number:\n"
    "1. Scan EVERY row of the table, not just the first row or a fixed position.\n"
)

_FUND_TABLE_GUIDANCE_SINGLE_TABLE = (
    "2. Identify the SELECTED fund: the row that has a handwritten amount (or, on a "
    "Disinvestment form, a handwritten percentage) filled in, and/or a tick in that "
    "row's Income Distribution (Reinvest / Pay out) columns if present. 'N/A' printed "
    "in a cell means that fund does not offer that option and is NOT itself a selection "
    "signal.\n"
    "3. fund_number is the handwritten digit string on that same row - do NOT confuse "
    "it with the deposit/withdrawal amount or percentage itself.\n"
    "4. fund_name is the PRINTED fund name text of that same row, not the risk-appetite "
    "header text above it (e.g. 'You are very careful and want to protect your capital').\n"
    "5. If more than one row appears to have amounts filled in, pick the row with the "
    "most complete evidence (amount/percentage AND a nearby handwritten fund number) and "
    "lower your confidence score accordingly.\n"
    "6. If the fund number truly cannot be found anywhere on the page, still return the "
    "fund_name from the selected row and set fund_number to null rather than inventing one.\n"
)

_FUND_TABLE_GUIDANCE_DEBIT = (
    "2. This Debit Order form has up to THREE separate fund tables, only one of which is "
    "in use - first check which instruction was ticked in section 3/4 "
    "('Cancel my existing debit order(s)', 'Changes to my existing debit order(s)', or "
    "section 4 'New debit order instructions'), then read ONLY the table under that ticked "
    "instruction:\n"
    "   - Cancel table columns: Bifm Unit Trust Fund(s) | Fund number | Cancellation date.\n"
    "   - Change table columns: Bifm Unit Trust Fund(s) | Fund number | Increase to (BWP) | "
    "Decrease to (BWP) | ** Existing amount.\n"
    "   - New debit order table columns: Bifm Unit Trust Fund(s) | Fund number | New amount (BWP).\n"
    "3. Within that one active table, the SELECTED fund is the row with a handwritten Fund "
    "number and/or a handwritten value in ANY of that table's amount/date columns (Increase "
    "to, Decrease to, Existing amount, New amount, or Cancellation date). A row with only a "
    "Fund number and no amount can still be the selected row if amounts genuinely weren't "
    "captured - use judgement and lower confidence when evidence is thin.\n"
    "4. fund_number is the handwritten digit string in that row's 'Fund number' column - do "
    "NOT confuse it with any of the amount columns.\n"
    "5. fund_name is the PRINTED fund name text of that same row (e.g. 'Bifm Pula Money "
    "Market Fund'), ignoring '*'/'**' markers.\n"
    "6. If the fund number truly cannot be found, still return the fund_name from the "
    "selected row and set fund_number to null rather than inventing one.\n"
)


def _fund_table_guidance(form_code: str) -> str:
    if form_code == "DEBIT":
        return _FUND_TABLE_GUIDANCE_COMMON + _FUND_TABLE_GUIDANCE_DEBIT
    return _FUND_TABLE_GUIDANCE_COMMON + _FUND_TABLE_GUIDANCE_SINGLE_TABLE


def _build_field_prompt(field_subset: list[dict], form_label: str, form_code: str = "") -> str:
    lines = []
    needs_fund_table_guidance = False
    for f in field_subset:
        opts = f" Allowed values: {f['options']}." if "options" in f else ""
        cond = f" (Only if: {f['condition']})" if f.get("condition") else ""
        lines.append(f"- {f['id']} ({f['label']}, type={f['type']}).{opts}{cond}")
        if f.get("extraction_hint") == _FUND_TABLE_HINT:
            needs_fund_table_guidance = True
    field_block = "\n".join(lines)

    fund_guidance = _fund_table_guidance(form_code) if needs_fund_table_guidance else ""

    return (
        f"You are an expert document examiner transcribing a HANDWRITTEN BIFM Unit Trust "
        f"'{form_label}' form. Read it the way a careful human clerk would:\n"
        "- Look at each letter/digit individually; don't guess from word shape alone.\n"
        "- Pay special attention to easily confused digits: 0/O, 1/7, 5/6, 8/3.\n"
        "- Use surrounding context (field label, expected format) to disambiguate.\n"
        "- For checkbox/tick-box fields, look for a tick, cross, circle, or shading INSIDE the box.\n"
        "- If a value is genuinely illegible or the field is blank, use null rather than guessing.\n"
        "- Give a LOWER confidence score (<60) for any field where handwriting was hard to read.\n"
        f"{fund_guidance}\n"
        f"FIELDS TO EXTRACT:\n{field_block}\n\n"
        "Also extract any beneficiary table rows (name, relationship, split_percent).\n\n"
        "Respond ONLY with this exact JSON shape:\n"
        "{\n"
        '  "fields": {"<field_id>": {"value": <value or null>, "confidence": <0-100>}, ...},\n'
        '  "beneficiaries": [{"name": "...", "relationship": "...", "split_percent": <number>}, ...]\n'
        "}"
    )


# ---------------------------------------------------------------------------
# Core extraction call
# ---------------------------------------------------------------------------

def _extract_fields_from_page(
    image_path: Path,
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    prompt = _build_field_prompt(field_subset, form_label, form_code)
    response = ask_vision(prompt, image_path, json_mode=True)

    try:
        parsed = parse_json_response(response.text)
    except Exception as exc:  # noqa: BLE001
        logger.error("Field extraction JSON parse failed for %s: %s | raw=%s", image_path.name, exc, response.text)
        return {}, []

    fields: dict[str, FieldValue] = {}
    for fid, payload in parsed.get("fields", {}).items():
        if payload is None:
            continue
        value = payload.get("value") if isinstance(payload, dict) else payload
        confidence = cap_confidence(payload.get("confidence", 0)) if isinstance(payload, dict) else 0.0
        if value is not None:
            fields[fid] = FieldValue(
                field_id=fid,
                value=value,
                confidence=confidence,
                source_page=_page_number(image_path),
            )

    beneficiaries = [
        Beneficiary(
            name=b.get("name", ""),
            relationship=b.get("relationship", ""),
            split_percent=float(b.get("split_percent", 0) or 0),
        )
        for b in parsed.get("beneficiaries", [])
        if b.get("name")
    ]
    return fields, beneficiaries


def _page_number(image_path: Path) -> int:
    try:
        return int(image_path.stem.split("_")[-1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Shared truthiness helpers for checkbox / amount-sourced fields
# ---------------------------------------------------------------------------

_CHECKED_TOKENS = {"true", "yes", "y", "1", "checked", "ticked", "x", "✓", "on"}


def _is_checked(value) -> bool:
    """
    Normalizes a checkbox-sourced field value to True/False. Vision-LLM
    extraction can hand back a native JSON boolean, or any of several
    string spellings for "ticked" (e.g. "Yes", "Checked", "X", "1") - this
    is the single place that vocabulary is defined so every checkbox field
    (account_closure, KYC document flags, etc.) is interpreted consistently
    instead of each call site inventing its own partial token list.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _CHECKED_TOKENS


def _has_amount(fv: FieldValue | None) -> bool:
    """
    True if a currency/percentage FieldValue actually carries a usable
    amount. Deliberately distinct from a plain truthiness check on
    fv.value: a legitimate extracted amount of 0 is falsy in Python
    (`0 == False`), and blank/placeholder strings like "", "N/A", "-"
    should NOT count as an amount having been written in that column.
    """
    if fv is None or fv.value is None:
        return False
    text = str(fv.value).strip()
    if text == "" or text.lower() in ("n/a", "na", "-", "none"):
        return False
    stripped = re.sub(r"[^0-9.\-]", "", text)
    if stripped in ("", "-", "."):
        # Non-numeric but non-blank (extraction glitch/handwriting noise) -
        # still treat as "something was written here" rather than
        # silently dropping it from consideration.
        return True
    try:
        return float(stripped) != 0
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Derived metadata fields
# ---------------------------------------------------------------------------

def _derive_metadata(form_code: str, fields: dict[str, FieldValue]) -> dict[str, FieldValue]:
    """
    Compute all metadata fields that must be generated by the system
    (not extracted from the form directly), per the requirements doc.
    """
    derived: dict[str, FieldValue] = {}

    def _add(field_id: str, value, confidence: float = CONFIDENCE_CEILING) -> None:
        derived[field_id] = FieldValue(field_id=field_id, value=value, confidence=confidence)

    # Form type label
    from app.utils.config_loader import load_field_definitions
    form_defs = load_field_definitions()
    form_label = form_defs.get(form_code, {}).get("label", form_code)
    _add("form_type", form_label)

    # Fund category & processing cut-off (applies to forms with fund selection)
    fund_name = None
    for fid in ("fund_name",):
        fv = fields.get(fid)
        if fv and fv.value:
            fund_name = str(fv.value)
            break

    if form_code == "DIS_GSG":
        # GSGF form always has the GSGF-specific cut-off
        _add("fund_name", "BIFM Global Sustainable Growth Fund")
        _add("fund_category", "Non-Money Market (GSGF)")
        _add("processing_cutoff", "Quarterly - 7th of last month of quarter")
        _add("fund_category_priority", fund_category_priority("Non-Money Market (GSGF)"))
    elif fund_name:
        fund_category, cutoff = derive_fund_category(fund_name)
        _add("fund_category", fund_category)
        _add("processing_cutoff", cutoff)
        _add("fund_category_priority", fund_category_priority(fund_category))
    elif form_code in ("ADD", "DIS", "DEBIT"):
        _add("fund_category", "Unknown - Fund Name Not Extracted")
        _add("processing_cutoff", "Unknown")
        _add("fund_category_priority", fund_category_priority(None))

    # Instruction mode for DIS / DIS_GSG - one of three mutually exclusive
    # modes, in priority order:
    #   1. Full Closure     - the "close account" checkbox at the foot of
    #                         the form is ticked. This overrides everything
    #                         else: an investor closing the account may
    #                         still have stray digits in the amount/percent
    #                         boxes, but the tick is the authoritative signal.
    #   2. Percentage       - the dedicated disinvestment_percentage column
    #                         has a number written in it.
    #   3. Partial Withdrawal - a currency amount is written in the
    #                         deposit/withdrawal (disinvestment_amount)
    #                         column, with no closure tick and no percentage.
    #   Unknown             - none of the above could be determined -
    #                         surfaced so it gets flagged for manual review
    #                         rather than silently defaulting to a mode.
    if form_code in ("DIS", "DIS_GSG"):
        closure = fields.get("account_closure")
        dis_amount = fields.get("disinvestment_amount")
        dis_pct = fields.get("disinvestment_percentage")

        if _is_checked(closure.value if closure else None):
            _add("instruction_mode", "Full Closure")
        elif _has_amount(dis_pct):
            _add("instruction_mode", "Percentage")
        elif _has_amount(dis_amount):
            _add("instruction_mode", "Partial Withdrawal")
        else:
            _add("instruction_mode", "Unknown", confidence=0.0)

    # Sub-instruction type for DEBIT
    if form_code == "DEBIT":
        instr = fields.get("instruction_type")
        if instr and instr.value:
            val = str(instr.value).lower()
            if "cancel" in val:
                _add("sub_instruction_type", "Cancel")
            elif "change" in val:
                _add("sub_instruction_type", "Change")
            elif "new" in val:
                _add("sub_instruction_type", "New")
            else:
                _add("sub_instruction_type", instr.value)

    # Sub-type for STATIC
    if form_code == "STATIC":
        change_type = fields.get("change_type")
        if change_type and change_type.value:
            _add("static_sub_type", change_type.value)

    # KYC completeness flag
    if form_code == "KYC":
        kyc_fields = ["kyc_certified_id", "kyc_proof_address", "kyc_proof_banking", "kyc_proof_source"]
        ticked = [
            fid for fid in kyc_fields
            if fields.get(fid) and _is_checked(fields[fid].value)
        ]
        all_complete = len(ticked) == 4
        _add("kyc_completeness_flag", "Complete" if all_complete else f"Incomplete ({len(ticked)}/4 documents provided)")

    return derived


# ---------------------------------------------------------------------------
# Fund field cleanup
# ---------------------------------------------------------------------------

def _clean_fund_fields(fields: dict[str, FieldValue]) -> None:
    """
    Normalizes fund_name / fund_number in place after extraction from the
    Core Fund Range table:
      - Strips leading '*' / '**' risk-tier markers and stray whitespace from
        fund_name (these are printed annotations on the form, not part of
        the fund's actual name).
      - Strips non-digit characters from fund_number while preserving the
        digit string exactly as written (8 or 9 digits are both valid for
        BIFM fund/entity numbers - no padding or truncation).
    """
    import re as _re

    fund_name_fv = fields.get("fund_name")
    if fund_name_fv and fund_name_fv.value:
        cleaned = _re.sub(r"^\**\s*", "", str(fund_name_fv.value)).strip()
        fund_name_fv.value = cleaned

    fund_number_fv = fields.get("fund_number")
    if fund_number_fv and fund_number_fv.value:
        digits = _re.sub(r"\D", "", str(fund_number_fv.value))
        if digits:
            fund_number_fv.value = digits


# ---------------------------------------------------------------------------
# Public entry points (one per form type, plus the routing dispatcher)
# ---------------------------------------------------------------------------

def extract_form(form_code: str, page_images: list[Path]) -> ExtractionResult:
    """
    Routes to the correct extraction logic based on form_code.
    This is the single entry point called by pipeline.py.
    """
    if not page_images:
        raise ValueError(f"No page images provided for extraction (form_code={form_code})")

    if form_code == "APPFORM":
        return _extract_appform(page_images)
    else:
        return _extract_standard_form(form_code, page_images)


def _extract_appform(page_images: list[Path]) -> ExtractionResult:
    """
    Investment Application Form extractor.
    Uses page 1 for primary fields; page 10 conditionally for guardian/minor details.
    """
    field_defs = get_fields_for_form("APPFORM")
    primary_fields = [f for f in field_defs if f.get("page_hint") != "Page 10" and f["id"] not in GUARDIAN_FIELD_IDS]
    guardian_fields = [f for f in field_defs if f["id"] in GUARDIAN_FIELD_IDS or f.get("page_hint") == "Page 10"]

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    # Pass 1: primary page
    fields, beneficiaries = _extract_fields_from_page(page_images[0], primary_fields, "Investment Application Form")
    all_fields.update(fields)
    all_beneficiaries.extend(beneficiaries)

    # Pass 2 (conditional): guardian/minor section on page 10
    account_type = all_fields.get("account_type")
    is_minor_account = account_type is not None and "behalf" in str(account_type.value).lower()
    if is_minor_account and len(page_images) >= GUARDIAN_PAGE_NUMBER:
        guardian_page = page_images[GUARDIAN_PAGE_NUMBER - 1]
        g_fields, _ = _extract_fields_from_page(guardian_page, guardian_fields, "Investment Application Form (Guardian Section)")
        all_fields.update(g_fields)
    elif is_minor_account:
        logger.warning(
            "Account flagged 'Acting on Behalf of' but document has only %d page(s); "
            "expected guardian section on page %d.", len(page_images), GUARDIAN_PAGE_NUMBER,
        )

    if "id_type" not in all_fields and "id_number" in all_fields:
        logger.warning("id_type not extracted directly - downstream validation will treat it as unknown")

    _clean_fund_fields(all_fields)
    all_fields.update(_derive_metadata("APPFORM", all_fields))

    result = ExtractionResult(
        source_file=page_images[0].parent.name,
        form_code="APPFORM",
        fields=all_fields,
        beneficiaries=all_beneficiaries,
        page_count=len(page_images),
    )
    logger.info("Extracted %d field(s) and %d beneficiary row(s) from %s (APPFORM)",
                len(all_fields), len(all_beneficiaries), result.source_file)
    return result


def _extract_standard_form(form_code: str, page_images: list[Path]) -> ExtractionResult:
    """
    General extractor for ADD, DEBIT, DIS, DIS_GSG, STATIC, KYC.
    Sends the first two pages in one vision call each and merges the result
    (page 1 is a cover/instructions page with no form data on the BIFM
    template for every form here except KYC, where page 1 is the form
    itself - see the pages_to_process comment below).
    """
    field_defs = get_fields_for_form(form_code)
    form_label = load_field_definitions().get(form_code, {}).get("label", form_code)

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    # For STATIC forms that may span two pages (Form A and Form B are separate sections),
    # run extraction on up to 2 pages and merge.
    # BIFM's own PDF templates put a cover/instructions page ("please read
    # carefully", cut-off times) FIRST on every form except KYC - the actual
    # Investor Details + fund table + banking section is page 2. Sending
    # only page_images[0] for DIS/ADD/DEBIT/DIS_GSG means the model is shown
    # a page with no investor data on it at all, and returns nothing for
    # every field. KYC is the one form whose page 1 genuinely is the form.
    if form_code == "KYC":
        pages_to_process = page_images[:1]
    else:
        pages_to_process = page_images[:2]

    for page_img in pages_to_process:
        fields, beneficiaries = _extract_fields_from_page(page_img, field_defs, form_label, form_code)
        # Merge: don't overwrite a higher-confidence extraction from a prior page
        for fid, fv in fields.items():
            existing = all_fields.get(fid)
            if existing is None or fv.confidence > existing.confidence:
                all_fields[fid] = fv
        all_beneficiaries.extend(beneficiaries)

    # Normalize fund_name / fund_number pulled from the Core Fund Range table
    # (strip '*'/'**' risk-tier markers, strip non-digits from fund_number)
    # before deriving metadata that depends on fund_name (fund_category, cutoff).
    _clean_fund_fields(all_fields)

    # Append system-derived metadata fields
    all_fields.update(_derive_metadata(form_code, all_fields))

    result = ExtractionResult(
        source_file=page_images[0].parent.name,
        form_code=form_code,
        fields=all_fields,
        beneficiaries=all_beneficiaries,
        page_count=len(page_images),
    )
    logger.info("Extracted %d field(s) from %s (%s)", len(all_fields), result.source_file, form_code)
    return result


# Keep backward-compatible alias used by older pipeline code
def extract_investment_application_form(page_images: list[Path]) -> ExtractionResult:
    """Backward-compatible alias for APPFORM extraction."""
    return _extract_appform(page_images)
