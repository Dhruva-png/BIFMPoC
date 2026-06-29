"""
Module 2: Field Extractor - Investment Application Form (Section 7.2).

Cost-conscious design: ONE llava call covers the primary investor-details
pages (page 1, where nearly all mandatory fields live). A SECOND call is
only made for the Page 10 guardian/authorisation section, and only when the
first pass reports Account Type = "Acting on Behalf of" - i.e. most adult
individual applications cost exactly one vision call, matching the
document's own page_hint metadata in config/field_definitions.json.
"""
from __future__ import annotations

from pathlib import Path

from app.llm.ollama_client import ask_vision, parse_json_response
from app.models.schemas import Beneficiary, ExtractionResult, FieldValue
from app.utils.config_loader import load_field_definitions
from app.utils.logger import get_logger

logger = get_logger(__name__)

GUARDIAN_FIELD_IDS = {"guardian_name", "guardian_id_number"}
GUARDIAN_PAGE_NUMBER = 10  # per Section 4.2 "Minor / Child Account Logic"


def _build_field_prompt(field_subset: list[dict]) -> str:
    lines = []
    for f in field_subset:
        opts = f" Allowed values: {f['options']}." if "options" in f else ""
        lines.append(f"- {f['id']} ({f['label']}, type={f['type']}).{opts}")
    field_block = "\n".join(lines)

    return (
        "You are an expert document examiner transcribing a HANDWRITTEN BIFM Unit Trust "
        "form. The applicant's handwriting may be cursive, inconsistent, or partially "
        "unclear - read it the way a careful human clerk would:\n"
        "- Look at each letter/digit individually before deciding a value; don't guess from word shape alone.\n"
        "- Pay special attention to digits that are easy to confuse: 0/O, 1/7, 5/6, 8/3.\n"
        "- Use surrounding context (field label, expected format) to disambiguate, but never invent "
        "a value that doesn't match the strokes on the page.\n"
        "- For checkbox/tick-box fields, look for a tick, cross, circle, or shading inside the box - "
        "not just print near it.\n"
        "- If a value is genuinely illegible or the field is blank, use null rather than guessing.\n"
        "- Give a LOWER confidence score (below 60) for any field where the handwriting was hard to "
        "read, even if you produced a best-guess value - confidence should reflect how sure you are, "
        "not just whether you filled in something.\n\n"
        f"FIELDS:\n{field_block}\n\n"
        "Also extract any beneficiary table rows you see, as a list of objects with "
        "name, relationship, split_percent.\n\n"
        "Respond ONLY with a JSON object of this exact shape:\n"
        "{\n"
        '  "fields": {"<field_id>": {"value": <value or null>, "confidence": <0-100 integer>}, ...},\n'
        '  "beneficiaries": [{"name": "...", "relationship": "...", "split_percent": <number>}, ...]\n'
        "}"
    )


def _extract_fields_from_page(image_path: Path, field_subset: list[dict]) -> tuple[dict[str, FieldValue], list[Beneficiary]]:
    prompt = _build_field_prompt(field_subset)
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
            fields[fid] = FieldValue(field_id=fid, value=value, confidence=confidence, source_page=_page_number(image_path))

    beneficiaries = [
        Beneficiary(name=b.get("name", ""), relationship=b.get("relationship", ""), split_percent=float(b.get("split_percent", 0) or 0))
        for b in parsed.get("beneficiaries", [])
        if b.get("name")
    ]
    return fields, beneficiaries


def _page_number(image_path: Path) -> int:
    # filenames are page_001.png, page_002.png, ...
    try:
        return int(image_path.stem.split("_")[-1])
    except (IndexError, ValueError):
        return 0


def extract_investment_application_form(page_images: list[Path]) -> ExtractionResult:
    """
    page_images must be in page order, as produced by app.ocr.pdf_utils.render_pdf_to_images.
    """
    field_defs = load_field_definitions()["fields"]
    primary_fields = [f for f in field_defs if f.get("page_hint") != "Page 10" and f["id"] not in GUARDIAN_FIELD_IDS]
    guardian_fields = [f for f in field_defs if f["id"] in GUARDIAN_FIELD_IDS or f.get("page_hint") == "Page 10"]

    if not page_images:
        raise ValueError("No page images provided for extraction")

    all_fields: dict[str, FieldValue] = {}
    all_beneficiaries: list[Beneficiary] = []

    # Pass 1: primary investor-details page (page 1 covers nearly all mandatory fields)
    primary_page = page_images[0]
    fields, beneficiaries = _extract_fields_from_page(primary_page, primary_fields)
    all_fields.update(fields)
    all_beneficiaries.extend(beneficiaries)

    # Pass 2 (conditional): only if this is a minor/guardian account, per Section 4.2.
    account_type = all_fields.get("account_type")
    is_minor_account = account_type is not None and "behalf" in str(account_type.value).lower()

    if is_minor_account and len(page_images) >= GUARDIAN_PAGE_NUMBER:
        guardian_page = page_images[GUARDIAN_PAGE_NUMBER - 1]
        g_fields, _ = _extract_fields_from_page(guardian_page, guardian_fields)
        all_fields.update(g_fields)
    elif is_minor_account:
        logger.warning(
            "Account flagged 'Acting on Behalf of' but document has only %d page(s); "
            "expected guardian section on page %d.", len(page_images), GUARDIAN_PAGE_NUMBER,
        )

    # Derive id_type from whichever ID-type checkbox value was captured, if not explicit.
    if "id_type" not in all_fields and "id_number" in all_fields:
        logger.warning("id_type not extracted directly - downstream validation will treat it as unknown")

    result = ExtractionResult(
        source_file=primary_page.parent.name,
        form_code="APPFORM",
        fields=all_fields,
        beneficiaries=all_beneficiaries,
        page_count=len(page_images),
    )
    logger.info("Extracted %d field(s) and %d beneficiary row(s) from %s", len(all_fields), len(all_beneficiaries), result.source_file)
    return result
