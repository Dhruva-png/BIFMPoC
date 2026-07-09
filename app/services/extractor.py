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
- Each form type's field list comes from config/field_definitions.json so
  the prompt is always form-specific — not a single generic mega-prompt that
  confuses the model with fields that don't exist on the current form.
- After extraction, derived metadata fields are computed deterministically:
    fund_category, processing_cutoff, form_type_label, instruction_mode,
    kyc_completeness_flag, sub_instruction_type.

MULTI-PASS SELF-CONSISTENCY EXTRACTION (settings.multi_pass_extraction,
default on):
Every page is read TWICE, independently, and the two reads are compared
field-by-field:
  - Agree (after whitespace/case normalization)  -> confidence pushed up
    toward the ceiling; this is real evidence the value is right, not
    just the model's own stated confidence.
  - Disagree                                     -> confidence capped low
    and a third, focused tie-break pass is fired ONLY for the disputed
    field(s), on a further-upscaled/sharpened image, with a prompt that
    shows the model both candidate reads and asks it to look very
    carefully at just that field.
  - Only one pass saw the field at all            -> confidence discounted
    slightly (one read isn't as solid as two agreeing reads).

Pass 1 uses the enhanced page image; pass 2 uses the raw, un-enhanced
sibling saved by app.ocr.pdf_utils (enhancement helps in most cases but
occasionally over-sharpens noise into a false stroke — a genuinely
different image is what makes this a real independent check rather than
asking the same question twice). If no raw sibling exists (enhancement
was off for this run), multi-pass falls back to a single read rather than
asking the identical image the identical question twice at temperature 0,
which would just return the same answer and add nothing.

This roughly doubles vision-API calls per document (triples only for
fields that end up disputed) — see config.settings.AppSettings.
multi_pass_extraction to turn it off if you need to conserve Groq TPM
budget on a large batch.

On top of this, app.utils.field_validators runs a second, independent
check: does the extracted value actually look like a well-formed email /
phone / ID / percentage / currency, regardless of what confidence the
model reported for it. This catches misreads a model can be "confident"
about (e.g. a clean-looking but wrong digit).
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageEnhance

from app.llm.router import ask_vision, parse_json_response
from app.models.schemas import Beneficiary, ExtractionResult, FieldValue
from app.ocr.pdf_utils import raw_sibling_path
from app.utils.confidence import (
    AGREEMENT_CONFIDENCE_BONUS,
    CONFIDENCE_CEILING,
    DISAGREEMENT_CONFIDENCE_CAP,
    SINGLE_PASS_CONFIDENCE_FACTOR,
    cap_confidence,
)
from app.utils.config_loader import (
    derive_fund_category,
    fund_category_priority,
    get_fields_for_form,
    load_field_definitions,
)
from app.utils.field_validators import apply_field_validation
from app.utils.logger import get_logger
from config.settings import settings

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


def _build_field_prompt(
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
    tiebreak_candidates: dict[str, tuple] | None = None,
) -> str:
    lines = []
    needs_fund_table_guidance = False
    for f in field_subset:
        opts = f" Allowed values: {f['options']}." if "options" in f else ""
        cond = f" (Only if: {f['condition']})" if f.get("condition") else ""
        tiebreak_note = ""
        if tiebreak_candidates and f["id"] in tiebreak_candidates:
            a, b = tiebreak_candidates[f["id"]]
            tiebreak_note = (
                f" TWO INDEPENDENT READS OF THIS FIELD DISAGREED: {a!r} vs {b!r}. "
                "Slow down and look at individual letter/digit shapes for just this "
                "field - it may be either candidate, or it's possible both prior reads "
                "were wrong; if so, provide the value that's actually written."
            )
        lines.append(f"- {f['id']} ({f['label']}, type={f['type']}).{opts}{cond}{tiebreak_note}")
        if f.get("extraction_hint") == _FUND_TABLE_HINT:
            needs_fund_table_guidance = True
    field_block = "\n".join(lines)

    fund_guidance = _fund_table_guidance(form_code) if needs_fund_table_guidance else ""

    if tiebreak_candidates:
        intro = (
            f"You are re-examining SPECIFIC fields on a HANDWRITTEN BIFM Unit Trust "
            f"'{form_label}' form where two independent reads disagreed. This is a "
            "focused recheck of only the field(s) below, not a fresh full read - go "
            "slowly.\n"
            "- Pay special attention to easily confused digits: 0/O, 1/7, 5/6, 8/3.\n"
            "- Use surrounding context (field label, expected format) to disambiguate.\n"
            "- If the value is genuinely illegible, use null rather than guessing.\n"
        )
    else:
        intro = (
            f"You are an expert document examiner transcribing a HANDWRITTEN BIFM Unit Trust "
            f"'{form_label}' form. Read it the way a careful human clerk would:\n"
            "- Look at each letter/digit individually; don't guess from word shape alone.\n"
            "- Pay special attention to easily confused digits: 0/O, 1/7, 5/6, 8/3.\n"
            "- Use surrounding context (field label, expected format) to disambiguate.\n"
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


# ---------------------------------------------------------------------------
# Single vision call (one image, one prompt)
# ---------------------------------------------------------------------------

def _single_vision_pass(
    image_path: Path,
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
    tiebreak_candidates: dict[str, tuple] | None = None,
) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    prompt = _build_field_prompt(field_subset, form_label, form_code, tiebreak_candidates)
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
        return int(image_path.stem.split("_")[0 - 1].replace("raw", "").replace("tiebreak", "") or 0)
    except (IndexError, ValueError):
        # Fall back to the leading numeric run in the stem (handles
        # "page_003", "page_003_raw", "page_003_tiebreak" alike).
        m = re.search(r"page_(\d+)", image_path.stem)
        return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Multi-pass self-consistency merge
# ---------------------------------------------------------------------------

def _normalize_for_compare(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _merge_passes(
    fields_a: dict[str, FieldValue],
    fields_b: dict[str, FieldValue],
) -> tuple[dict[str, FieldValue], set[str]]:
    """
    Compares two independent extraction passes field-by-field. Returns the
    merged field set plus the set of field_ids that disagreed (candidates
    for a tie-break pass). See module docstring for the confidence math.
    """
    merged: dict[str, FieldValue] = {}
    disagreements: set[str] = set()

    for fid in set(fields_a) | set(fields_b):
        fa = fields_a.get(fid)
        fb = fields_b.get(fid)

        if fa and fb:
            if _normalize_for_compare(fa.value) == _normalize_for_compare(fb.value):
                winner = fa if fa.confidence >= fb.confidence else fb
                boosted = cap_confidence(max(fa.confidence, fb.confidence) + AGREEMENT_CONFIDENCE_BONUS)
                merged[fid] = FieldValue(
                    field_id=fid, value=winner.value, confidence=boosted,
                    source_page=winner.source_page, source=winner.source,
                )
            else:
                disagreements.add(fid)
                fallback = fa if fa.confidence >= fb.confidence else fb
                merged[fid] = FieldValue(
                    field_id=fid, value=fallback.value,
                    confidence=min(fallback.confidence, DISAGREEMENT_CONFIDENCE_CAP),
                    source_page=fallback.source_page, source=fallback.source,
                )
        else:
            only = fa or fb
            merged[fid] = FieldValue(
                field_id=fid, value=only.value,
                confidence=cap_confidence(only.confidence * SINGLE_PASS_CONFIDENCE_FACTOR),
                source_page=only.source_page, source=only.source,
            )

    return merged, disagreements


def _make_tiebreak_variant(image_path: Path) -> Path:
    """
    Produces a further-upscaled, extra-sharpened variant of an already-
    enhanced page image, used only for the tie-break pass on fields where
    the primary and secondary reads disagreed. Cached alongside the
    source image so repeated tie-breaks within the same run reuse it.
    """
    out_path = image_path.parent / f"{image_path.stem}_tiebreak{image_path.suffix}"
    if out_path.exists():
        return out_path
    img = Image.open(image_path)
    img = img.resize((int(img.width * 1.5), int(img.height * 1.5)), Image.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img.save(out_path, "PNG")
    return out_path


def _extract_fields_from_page(
    image_path: Path,
    field_subset: list[dict],
    form_label: str,
    form_code: str = "",
) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    """
    Multi-pass, self-consistency extraction for one page. See module
    docstring for the full explanation. Falls back to a single pass when
    multi_pass_extraction is off, or when no raw sibling image is
    available to serve as a genuinely independent second read.
    """
    fields_a, beneficiaries = _single_vision_pass(image_path, field_subset, form_label, form_code)

    if not settings.multi_pass_extraction:
        return fields_a, beneficiaries

    raw_path = raw_sibling_path(image_path)
    if raw_path is None:
        return fields_a, beneficiaries

    fields_b, beneficiaries_b = _single_vision_pass(raw_path, field_subset, form_label, form_code)
    if not beneficiaries and beneficiaries_b:
        beneficiaries = beneficiaries_b

    merged, disagreement_ids = _merge_passes(fields_a, fields_b)

    if disagreement_ids:
        tiebreak_subset = [f for f in field_subset if f["id"] in disagreement_ids]
        candidates = {
            fid: (
                fields_a[fid].value if fid in fields_a else None,
                fields_b[fid].value if fid in fields_b else None,
            )
            for fid in disagreement_ids
        }
        try:
            zoom_path = _make_tiebreak_variant(image_path)
            fields_c, _ = _single_vision_pass(
                zoom_path, tiebreak_subset, form_label, form_code, tiebreak_candidates=candidates,
            )
            for fid, fv in fields_c.items():
                merged[fid] = fv
            logger.info(
                "%s: tie-break resolved %d/%d disputed field(s): %s",
                image_path.name, len(fields_c), len(disagreement_ids), ", ".join(disagreement_ids),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Tie-break pass failed for %s - keeping capped disagreement value(s) for: %s",
                image_path.name, ", ".join(disagreement_ids),
            )

    return merged, beneficiaries


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
        digit string exactly as written (BIFM fund/entity numbers can range
        from 7 to 13 digits - no padding or truncation).
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

def extract_form(form_code: str, pages: dict[int, Path]) -> ExtractionResult:
    """
    Routes to the correct extraction logic based on form_code.
    This is the single entry point called by pipeline.py.

    pages: {1-indexed page number: rendered image path}, e.g. {1: ..., 10:
    ...} for an APPFORM's primary + guardian pages. Keyed by real page
    number rather than a positionally-dense list, since pipeline.py only
    ever renders the specific pages a form type needs (see
    app.ocr.pdf_utils.render_pdf_to_images) - a 10-page APPFORM submission
    may only have pages {1, 10} rendered at all, with pages 2-9 never
    touched.
    """
    if not pages:
        raise ValueError(f"No page images provided for extraction (form_code={form_code})")

    if form_code == "APPFORM":
        return _extract_appform(pages)
    else:
        return _extract_standard_form(form_code, pages)


def _extract_appform(pages: dict[int, Path]) -> ExtractionResult:
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
    fields, beneficiaries = _extract_fields_from_page(pages[1], primary_fields, "Investment Application Form")
    all_fields.update(fields)
    all_beneficiaries.extend(beneficiaries)

    # Pass 2 (conditional): guardian/minor section on page 10
    account_type = all_fields.get("account_type")
    is_minor_account = account_type is not None and "behalf" in str(account_type.value).lower()
    guardian_page = pages.get(GUARDIAN_PAGE_NUMBER)
    if is_minor_account and guardian_page is not None:
        g_fields, _ = _extract_fields_from_page(guardian_page, guardian_fields, "Investment Application Form (Guardian Section)")
        all_fields.update(g_fields)
    elif is_minor_account:
        logger.warning(
            "Account flagged 'Acting on Behalf of' but page %d (guardian section) "
            "wasn't available in this document.", GUARDIAN_PAGE_NUMBER,
        )

    if "id_type" not in all_fields and "id_number" in all_fields:
        logger.warning("id_type not extracted directly - downstream validation will treat it as unknown")

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

    # BIFM's own PDF templates put a cover/instructions page ("please read
    # carefully", cut-off times) FIRST on every form except KYC - the actual
    # Investor Details + fund table + banking section is page 2. Sending
    # only page 1 for DIS/ADD/DEBIT/DIS_GSG means the model is shown a page
    # with no investor data on it at all, and returns nothing for every
    # field. KYC is the one form whose page 1 genuinely is the form.
    wanted_page_numbers = (1,) if form_code == "KYC" else (1, 2)
    pages_to_process = [pages[n] for n in wanted_page_numbers if n in pages]

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

    # Independent shape/format check (email, phone, numeric IDs,
    # percentage, currency) - adjusts confidence based on whether the
    # value actually looks well-formed, not just what the model reported.
    apply_field_validation(form_code, all_fields)

    # Append system-derived metadata fields
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