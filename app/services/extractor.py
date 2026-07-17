from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
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
from app.utils.field_validators import apply_field_validation
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

GUARDIAN_FIELD_IDS = {"guardian_name", "guardian_id_number"}
GUARDIAN_PAGE_NUMBER = 10

INVESTMENT_PAGE_NUMBER = 3
INVESTMENT_PAGE_HINT = "Page 3"

BANKING_PAGE_NUMBER = 4
BANKING_PAGE_HINT = "Page 4"

VISION_SEMAPHORE = threading.Semaphore(getattr(settings, "max_concurrent_vision_calls", 8))
_FUND_TABLE_HINT = "fund_table"

_FUND_TABLE_GUIDANCE_COMMON = (
    "\nSPECIAL INSTRUCTIONS FOR fund_name / fund_number (fund list table):\n"
    "This form lists SEVERAL BIFM Unit Trust funds as fixed rows in a table (e.g. "
    "'Bifm Pula Money Market Fund', '* Bifm Letlotlo Education Fund', 'Bifm Balanced "
    "Prudential Fund', '* Bifm Ya Masa Junior Fund', 'Bifm Local Equity Fund', "
    "'** Bifm Global Sustainable Growth Fund' — ignore the '*'/'**' lock-in markers "
    "when extracting the name). The fund name itself is PRINTED, not handwritten. "
    "The fund number is a handwritten digit string (anywhere from 7 to 13 digits — capture exactly "
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


_FORMAT_HINTS: dict[str, str] = {
    "id_number": "An Omang or Birth Certificate number is EXACTLY 9 digits, no letters; a passport number's format varies by country.",
    "guardian_id_number": "An Omang number: EXACTLY 9 digits, no letters.",
    "branch_code": "EXACTLY 6 digits.",
    "account_number": "Digits only.",
    "fund_number": "Digits only - capture exactly what is written, do not pad or truncate.",
}

_TYPE_FORMAT_HINTS: dict[str, str] = {
    "date": "Written as dd/mm/yyyy on the form.",
    "currency": "A BWP amount - digits only, no currency symbol or thousands separator.",
    "percentage": "A number between 0 and 100.",
    "email": "A standard email address.",
}


def _build_field_prompt(
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
) -> str:
    lines = []
    needs_fund_table_guidance = False
    for f in field_subset:
        opts = f" Allowed values: {f['options']}." if "options" in f else ""
        cond = f" (Only if: {f['condition']})" if f.get("condition") else ""
        hint = _FORMAT_HINTS.get(f["id"]) or _TYPE_FORMAT_HINTS.get(f.get("type", ""), "")
        hint = f" {hint}" if hint else ""
        lines.append(f"- {f['id']} ({f['label']}, type={f['type']}).{opts}{cond}{hint}")
        if f.get("extraction_hint") == _FUND_TABLE_HINT:
            needs_fund_table_guidance = True
    field_block = "\n".join(lines)

    fund_guidance = _fund_table_guidance(form_code) if needs_fund_table_guidance else ""

    intro = (
        f"You are an expert document examiner transcribing a HANDWRITTEN BIFM Unit Trust "
        f"'{form_label}' form. Read it the way a careful human clerk would:\n"
        "- Look at each letter/digit individually; don't guess from word shape alone.\n"
        "- Pay special attention to easily confused digits: 0/O, 1/7, 5/6, 8/3.\n"
        "- Use surrounding context (field label, expected format) to disambiguate.\n"
        "- Where a field states an exact expected format below, check your read against "
        "it before answering - if what you transcribed doesn't fit, look again.\n"
        "- Where a field lists allowed values, return one of them verbatim.\n"
        "- For checkbox/tick-box fields, look for a tick, cross, circle, or shading INSIDE the box.\n"
        "- If a value is genuinely illegible or the field is blank, use null rather than guessing.\n"
        "- Give a LOWER confidence score (<60) for any field where handwriting was hard to read.\n"
    )

    return (
        f"{intro}"
        f"{fund_guidance}\n"
        f"FIELDS TO EXTRACT:\n{field_block}\n\n"
        "Also extract any beneficiary table rows (name, relationship, split_percent).\n\n"
        "Respond ONLY with this exact JSON shape:\n"
        "{\n"
        '  "fields": {"<field_id>": {"value": <value or null>, "confidence": <0-100>}, ...},\n'
        '  "beneficiaries": [{"name": "...", "relationship": "...", "split_percent": <number>}, ...]\n'
        "}"
    )

def _extract_fields_from_page(
    image_path: Path,
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    """One vision call for one page. Accuracy beyond the model's own read
    comes from app.utils.field_repair's domain constraints, applied later
    in the pipeline by app.utils.field_validators - not from re-reading the
    page. See this module's docstring."""
    prompt = _build_field_prompt(field_subset, form_label, form_code)

    with VISION_SEMAPHORE:
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
        return int(image_path.stem.split("_")[0 - 1] or 0)
    except (IndexError, ValueError):
        m = re.search(r"page_(\d+)", image_path.stem)
        return int(m.group(1)) if m else 0


def _retry_missing_mandatory(
    form_code: str,
    field_defs: list[dict],
    page: Path | None,
    fields: dict[str, FieldValue],
    form_label: str,
) -> dict[str, FieldValue]:
    """
    If any of this form's MANDATORY fields came back blank after the normal
    read, re-ask ONCE with a prompt listing only those fields - a narrow
    prompt concentrates the model's attention on exactly the values that
    would otherwise auto-reject the instruction. Unlike the old multi-pass
    approach (every page read twice, always), this costs nothing on a clean
    document and at most one extra vision call on a bad one.
    """
    if page is None or not getattr(settings, "retry_missing_mandatory", True):
        return {}

    mandatory = set(get_mandatory_fields_for_form(form_code))

    def _blank(fid: str) -> bool:
        fv = fields.get(fid)
        return fv is None or fv.value is None or str(fv.value).strip() == ""

    missing_defs = [f for f in field_defs if f["id"] in mandatory and _blank(f["id"])]
    if not missing_defs:
        return {}

    missing_ids = [f["id"] for f in missing_defs]
    logger.info(
        "%s: mandatory field(s) missing after first read (%s) - one focused re-read of %s",
        form_code, ", ".join(missing_ids), page.name,
    )
    try:
        retry_fields, _ = _extract_fields_from_page(page, missing_defs, form_label, form_code)
    except Exception:  # noqa: BLE001 - the retry is best-effort; the first read stands
        logger.exception("Focused re-read failed for %s - keeping first-read results", page.name)
        return {}

    recovered = {
        fid: fv for fid, fv in retry_fields.items()
        if fid in set(missing_ids) and fv.value is not None and str(fv.value).strip() != ""
    }
    if recovered:
        logger.info("%s: focused re-read recovered %s", form_code, ", ".join(recovered))
    return recovered


# ---------------------------------------------------------------------------
# Shared truthiness helpers for checkbox / amount-sourced fields
# ---------------------------------------------------------------------------

_CHECKED_TOKENS = {"true", "yes", "y", "1", "checked", "ticked", "x", "✓", "on"}


def _is_checked(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _CHECKED_TOKENS


def _has_amount(fv: FieldValue | None) -> bool:
    if fv is None or fv.value is None:
        return False
    text = str(fv.value).strip()
    if text == "" or text.lower() in ("n/a", "na", "-", "none"):
        return False
    stripped = re.sub(r"[^0-9.\-]", "", text)
    if stripped in ("", "-", "."):
        return True
    try:
        return float(stripped) != 0
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Derived metadata fields
# ---------------------------------------------------------------------------

def _derive_metadata(form_code: str, fields: dict[str, FieldValue]) -> dict[str, FieldValue]:
    derived: dict[str, FieldValue] = {}

    def _add(field_id: str, value, confidence: float = CONFIDENCE_CEILING) -> None:
        derived[field_id] = FieldValue(field_id=field_id, value=value, confidence=confidence)

    form_defs = load_field_definitions()
    form_label = form_defs.get(form_code, {}).get("label", form_code)
    _add("form_type", form_label)

    fund_name = None
    for fid in ("fund_name",):
        fv = fields.get(fid)
        if fv and fv.value:
            fund_name = str(fv.value)
            break

    if form_code == "DIS_GSG":
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

    if form_code == "STATIC":
        change_type = fields.get("change_type")
        if change_type and change_type.value:
            _add("static_sub_type", change_type.value)

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
# Public entry points
# ---------------------------------------------------------------------------

def extract_form(form_code: str, pages: dict[int, Path]) -> ExtractionResult:
    if not pages:
        raise ValueError(f"No page images provided for extraction (form_code={form_code})")

    if form_code == "APPFORM":
        return _extract_appform(pages)
    else:
        return _extract_standard_form(form_code, pages)


def _extract_appform(pages: dict[int, Path]) -> ExtractionResult:
    field_defs = get_fields_for_form("APPFORM")
    guardian_fields = [f for f in field_defs if f["id"] in GUARDIAN_FIELD_IDS or f.get("page_hint") == "Page 10"]
    investment_fields = [f for f in field_defs if f.get("page_hint") == INVESTMENT_PAGE_HINT]
    banking_fields = [f for f in field_defs if f.get("page_hint") == BANKING_PAGE_HINT]
    _off_primary = (
        {f["id"] for f in guardian_fields}
        | {f["id"] for f in investment_fields}
        | {f["id"] for f in banking_fields}
    )
    primary_fields = [f for f in field_defs if f["id"] not in _off_primary]

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    fields, beneficiaries = _extract_fields_from_page(pages[1], primary_fields, "Investment Application Form")
    all_fields.update(fields)
    all_beneficiaries.extend(beneficiaries)

    # Investment Fund details live on page 3 (fund table, lump sum / debit
    # amounts, income distribution) - extracted here rather than the page-1
    # primary pass. Follows the same per-page pattern as the guardian
    # section below; if page 3 wasn't rendered for this document, these
    # optional fields are simply left blank.
    investment_page = pages.get(INVESTMENT_PAGE_NUMBER)
    if investment_fields and investment_page is not None:
        inv_fields, _ = _extract_fields_from_page(
            investment_page, investment_fields, "Investment Application Form (Investment Details)", "APPFORM",
        )
        all_fields.update(inv_fields)

    # Source of Funds + investor banking details live on page 4, same
    # per-page pattern.
    banking_page = pages.get(BANKING_PAGE_NUMBER)
    if banking_fields and banking_page is not None:
        b_fields, _ = _extract_fields_from_page(
            banking_page, banking_fields, "Investment Application Form (Banking & Source of Funds)", "APPFORM",
        )
        all_fields.update(b_fields)

    # Page 10 holds Form C (authorisation to act on behalf / joint /
    # motshelo) and the guardian/minor section. Form C is expected whenever
    # the account is Acting on Behalf, Joint, or Motshelo - not just for
    # minors - so page 10 is read for any of those investor types.
    account_type = all_fields.get("account_type")
    account_type_text = str(account_type.value).lower() if account_type is not None else ""
    needs_form_c = any(k in account_type_text for k in ("behalf", "joint", "motshelo"))
    guardian_page = pages.get(GUARDIAN_PAGE_NUMBER)
    if needs_form_c and guardian_page is not None:
        g_fields, _ = _extract_fields_from_page(guardian_page, guardian_fields, "Investment Application Form (Form C / Guardian Section)")
        all_fields.update(g_fields)
    elif needs_form_c:
        logger.warning(
            "Investor type '%s' expects Form C but page %d wasn't available in this document.",
            account_type.value if account_type is not None else "", GUARDIAN_PAGE_NUMBER,
        )

    if "id_type" not in all_fields and "id_number" in all_fields:
        logger.warning("id_type not extracted directly - downstream validation will treat it as unknown")

    # Every APPFORM mandatory field lives in the primary field set, so a
    # focused re-read (if any are still blank) targets the primary page.
    all_fields.update(_retry_missing_mandatory(
        "APPFORM", primary_fields, pages.get(1), all_fields, "Investment Application Form",
    ))

    _clean_fund_fields(all_fields)
    apply_field_validation("APPFORM", all_fields)
    all_fields.update(_derive_metadata("APPFORM", all_fields))

    result = ExtractionResult(
        source_file=pages[1].parent.name,
        form_code="APPFORM",
        fields=all_fields,
        beneficiaries=all_beneficiaries,
        page_count=len(pages),
    )
    logger.info("Extracted %d field(s) and %d beneficiary row(s) from %s (APPFORM)",
                len(all_fields), len(all_beneficiaries), result.source_file)
    return result


def _extract_standard_form(form_code: str, pages: dict[int, Path]) -> ExtractionResult:
    field_defs = get_fields_for_form(form_code)
    form_label = load_field_definitions().get(form_code, {}).get("label", form_code)

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    wanted_page_numbers = (1,) if form_code == "KYC" else (1, 2)
    pages_to_process = [pages[n] for n in wanted_page_numbers if n in pages]

    if len(pages_to_process) > 1:
        with ThreadPoolExecutor(max_workers=len(pages_to_process)) as pool:
            page_results = list(pool.map(
                lambda p: _extract_fields_from_page(p, field_defs, form_label, form_code),
                pages_to_process,
            ))
    else:
        page_results = [
            _extract_fields_from_page(p, field_defs, form_label, form_code)
            for p in pages_to_process
        ]

    for fields, beneficiaries in page_results:
        for fid, fv in fields.items():
            existing = all_fields.get(fid)
            if existing is None or fv.confidence > existing.confidence:
                all_fields[fid] = fv
        all_beneficiaries.extend(beneficiaries)

    # Standard forms carry their data on page 2 (page 1 is the cover /
    # instructions page); KYC's page 1 IS the form. Target the focused
    # re-read there if any mandatory field is still blank after the merge.
    retry_page = pages.get(1) if form_code == "KYC" else (pages.get(2) or pages.get(1))
    all_fields.update(_retry_missing_mandatory(
        form_code, field_defs, retry_page, all_fields, form_label,
    ))

    _clean_fund_fields(all_fields)
    apply_field_validation(form_code, all_fields)
    all_fields.update(_derive_metadata(form_code, all_fields))

    result = ExtractionResult(
        source_file=pages[1].parent.name,
        form_code=form_code,
        fields=all_fields,
        beneficiaries=all_beneficiaries,
        page_count=len(pages),
    )
    logger.info("Extracted %d field(s) from %s (%s)", len(all_fields), result.source_file, form_code)
    return result


latest_extraction_as_list: list[list] = []

def extraction_result_to_list(result: ExtractionResult, include_meta: bool = True) -> list[list]:
    global latest_extraction_as_list

    rows = [
        [field_id, fv.value]
        for field_id, fv in result.fields.items()
    ]

    if include_meta:
        rows.append(["source_file", result.source_file])
        rows.append(["form_code", result.form_code])

    latest_extraction_as_list = rows
    return rows