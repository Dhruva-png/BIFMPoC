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

from pathlib import Path

from app.llm.router import ask_vision, parse_json_response
from app.models.schemas import Beneficiary, ExtractionResult, FieldValue
from app.utils.config_loader import (
    derive_fund_category,
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

_FUND_AMOUNT_FIELD_IDS = {
    "lump_sum_deposit_amount", "lump_sum_debit_amount", "new_debit_amount",
    "change_amount", "disinvestment_amount",
}


def _build_field_prompt(field_subset: list[dict], form_label: str) -> str:
    lines = []
    field_ids = {f["id"] for f in field_subset}
    for f in field_subset:
        opts = f" Allowed values: {f['options']}." if "options" in f else ""
        cond = f" (Only if: {f['condition']})" if f.get("condition") else ""
        lines.append(f"- {f['id']} ({f['label']}, type={f['type']}).{opts}{cond}")
    field_block = "\n".join(lines)

    fund_table_note = ""
    if "fund_name" in field_ids or field_ids & _FUND_AMOUNT_FIELD_IDS:
        fund_table_note = (
            "\n\nFUND TABLE LAYOUT — read this carefully:\n"
            "These forms list funds in a table with one row per fund. In each row, the "
            "FUND NAME is in the cell immediately to the LEFT of the amount cell for that "
            "row — the amount column never has its own header naming the fund. To find the "
            "correct fund_name:\n"
            "1. Find the row where an amount has been written in, or a box/tick has been "
            "marked (this is the fund the investor selected).\n"
            "2. Read the fund name from the cell directly to the left of that amount/tick in "
            "the SAME row — do not use a fund name from a different row, and do not use a "
            "column header or the table title.\n"
            "3. If more than one row has an amount filled in, extract the fund_name/amount "
            "pair from the row with the largest or most clearly completed entry, and lower "
            "the confidence score to flag the ambiguity.\n"
            "4. If no row has an amount or tick, leave fund_name null rather than guessing "
            "from an empty row.\n"
        )

    return (
        f"You are an expert document examiner transcribing a HANDWRITTEN BIFM Unit Trust "
        f"'{form_label}' form. Read it the way a careful human clerk would:\n"
        "- Look at each letter/digit individually; don't guess from word shape alone.\n"
        "- Pay special attention to easily confused digits: 0/O, 1/7, 5/6, 8/3.\n"
        "- Use surrounding context (field label, expected format) to disambiguate.\n"
        "- For checkbox/tick-box fields, look for a tick, cross, circle, or shading INSIDE the box.\n"
        "- If a value is genuinely illegible or the field is blank, use null rather than guessing.\n"
        "- Give a LOWER confidence score (<60) for any field where handwriting was hard to read."
        f"{fund_table_note}\n"
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
) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    prompt = _build_field_prompt(field_subset, form_label)
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
        confidence = float(payload.get("confidence", 0)) if isinstance(payload, dict) else 0.0
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
# Derived metadata fields
# ---------------------------------------------------------------------------

def _derive_metadata(form_code: str, fields: dict[str, FieldValue]) -> dict[str, FieldValue]:
    """
    Compute all metadata fields that must be generated by the system
    (not extracted from the form directly), per the requirements doc.
    """
    derived: dict[str, FieldValue] = {}

    def _add(field_id: str, value, confidence: float = 100.0) -> None:
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
    elif fund_name:
        fund_category, cutoff = derive_fund_category(fund_name)
        _add("fund_category", fund_category)
        _add("processing_cutoff", cutoff)
    elif form_code in ("ADD", "DIS", "DEBIT"):
        _add("fund_category", "Unknown - Fund Name Not Extracted")
        _add("processing_cutoff", "Unknown")

    # Instruction mode for DIS (partial / percentage / full closure)
    if form_code in ("DIS", "DIS_GSG"):
        closure = fields.get("account_closure")
        dis_amount = fields.get("disinvestment_amount")
        dis_pct = fields.get("disinvestment_percentage")
        if closure and str(closure.value).lower() in ("yes", "true", "1"):
            _add("instruction_mode", "Full Closure")
        elif dis_pct and dis_pct.value:
            _add("instruction_mode", "Percentage")
        elif dis_amount and dis_amount.value:
            _add("instruction_mode", "Partial Amount")
        else:
            _add("instruction_mode", "Unknown")

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
            if fields.get(fid) and str(fields[fid].value).lower() in ("true", "yes", "1", "checked")
        ]
        all_complete = len(ticked) == 4
        _add("kyc_completeness_flag", "Complete" if all_complete else f"Incomplete ({len(ticked)}/4 documents provided)")

    return derived


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
    Sends ALL form fields in one vision call against page 1 (and page 2 if multi-page).
    For longer forms (STATIC has Form A + Form B sections), we send all pages.
    """
    field_defs = get_fields_for_form(form_code)
    form_label = load_field_definitions().get(form_code, {}).get("label", form_code)

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    # For STATIC forms that may span two pages (Form A and Form B are separate sections),
    # run extraction on up to 2 pages and merge.
    pages_to_process = page_images[:2] if form_code == "STATIC" else [page_images[0]]

    for page_img in pages_to_process:
        fields, beneficiaries = _extract_fields_from_page(page_img, field_defs, form_label)
        # Merge: don't overwrite a higher-confidence extraction from a prior page
        for fid, fv in fields.items():
            existing = all_fields.get(fid)
            if existing is None or fv.confidence > existing.confidence:
                all_fields[fid] = fv
        all_beneficiaries.extend(beneficiaries)

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


# ---------------------------------------------------------------------------
# MarvelAI integration helper
# ---------------------------------------------------------------------------
# Not used by the Streamlit front end. Converts an ExtractionResult's fields
# (the parsed LLM JSON, held as FieldValue objects) into a plain
# list-of-lists — e.g. [["full_name", "Dhruva"], ["id_number", "123456"]] —
# for hand-off to the MarvelAI pipeline. `latest_extraction_as_list` is
# refreshed every time `extraction_result_to_list` is called, so MarvelAI's
# code can simply do:
#
#   from app.services.extractor import extraction_result_to_list, latest_extraction_as_list
#   rows = extraction_result_to_list(result)
#   # or, after that call: app.services.extractor.latest_extraction_as_list

latest_extraction_as_list: list[list] = []


def extraction_result_to_list(result: ExtractionResult, include_meta: bool = True) -> list[list]:
    """
    Flattens an ExtractionResult's fields into [[field_id, value], ...].

    Args:
        result: the ExtractionResult produced by extract_form().
        include_meta: if True, also appends ["source_file", ...] and
            ["form_code", ...] rows so the batch/document context travels
            with the field data. Set False for field values only.

    Returns:
        A list of [field_id, value] pairs. Also cached on the module-level
        `latest_extraction_as_list` variable for easy access elsewhere.
    """
    global latest_extraction_as_list

    rows: list[list] = [[field_id, fv.value] for field_id, fv in result.fields.items()]

    if include_meta:
        rows.append(["source_file", result.source_file])
        rows.append(["form_code", result.form_code])

    latest_extraction_as_list = rows
    return rows
