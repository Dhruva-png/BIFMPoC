from __future__ import annotations

from pathlib import Path

from app.llm.router import ask_vision, parse_json_response
from app.models.schemas import ClassificationResult
from app.utils.config_loader import load_form_types
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _build_classification_prompt() -> str:
    form_types = load_form_types()["form_types"]
    type_list = "\n".join(
        f"- {ft['code']}: {ft['name']} — {ft['purpose']}. Key visual hints: {', '.join(ft['classification_hints'])}"
        for ft in form_types
    )
    return (
        "You are classifying a scanned BIFM Unit Trust form image. "
        "Look at the form title, section headings, checkboxes, and any fund table visible on this page. "
        "Choose exactly ONE form code from the list below that best matches what you see.\n\n"
        f"FORM TYPES:\n{type_list}\n\n"
        "Respond ONLY with a JSON object:\n"
        '{"form_code": "<one of the codes above>", "confidence": <0-100 integer>, "reason": "<short reason>"}'
    )


def classify_form(first_page_image: Path) -> ClassificationResult:
    """Runs the primary classification call against page 1 of the document."""
    form_types_lookup = {ft["code"]: ft["name"] for ft in load_form_types()["form_types"]}
    valid_codes = set(form_types_lookup.keys())
    prompt = _build_classification_prompt()

    response = ask_vision(prompt, first_page_image, json_mode=True)
    try:
        parsed = parse_json_response(response.text)
        code = parsed["form_code"]
        confidence = float(parsed.get("confidence", 0))
        if code not in valid_codes:
            logger.warning("Classifier returned unrecognized code '%s' - not one of the 6 known form types", code)
            code, confidence = "UNRECOGNIZED", 0.0

    except (KeyError, ValueError, Exception) as exc:  # noqa: BLE001
        # Same reasoning: if we can't even parse what the model said, we
        # genuinely don't know what this document is - treating that as
        # "APPFORM at 0% confidence" is no more honest than treating an
        # unrecognized code that way.
        logger.error(
            "Could not parse classifier output for %s: %s | raw=%s",
            first_page_image, exc, response.text,
        )
        code, confidence = "UNRECOGNIZED", 0.0

    name = form_types_lookup.get(code, "Unrecognized document")
    result = ClassificationResult(
        form_code=code,
        form_name=name,
        confidence=confidence,
        raw_model_output=response.text,
    )
    logger.info(
        "Classified %s as %s (%.0f%% confidence)",
        first_page_image.parent.name, code, confidence,
    )
    return result


def disambiguate_gsg_vs_standard(fund_table_image: Path) -> ClassificationResult:
    prompt = (
        "This page is a Disinvestment form. Look at the fund name(s) listed for withdrawal.\n\n"
        "If the ONLY fund listed is 'BIFM Global Sustainable Growth Fund' (or GSGF), "
        "this is the GSGF Disinvestment form (code DIS_GSG).\n"
        "If any other fund is listed, or multiple funds, this is the Standard Disinvestment form (code DIS).\n\n"
        'Respond ONLY with JSON: {"form_code": "DIS_GSG" or "DIS", "confidence": <0-100>, "reason": "<short reason>"}'
    )
    response = ask_vision(prompt, fund_table_image, json_mode=True)
    try:
        parsed = parse_json_response(response.text)
        code = parsed["form_code"]
        confidence = float(parsed.get("confidence", 0))
        if code not in ("DIS", "DIS_GSG"):
            code, confidence = "DIS", 50.0
    except Exception as exc:  # noqa: BLE001
        logger.error("GSG disambiguation parse failure: %s | raw=%s", exc, response.text)
        code, confidence = "DIS", 0.0

    name = "Disinvestment Form (GSGF)" if code == "DIS_GSG" else "Disinvestment Form (Standard)"
    return ClassificationResult(
        form_code=code,
        form_name=name,
        confidence=confidence,
        raw_model_output=response.text,
    )