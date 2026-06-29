"""
Module 1: Form Classifier (Section 7.2).

Cost-conscious design: classification needs exactly ONE llava call on the
first page image for almost every form. Only when the result is ambiguous
between Disinvestment (Standard) and Disinvestment (GSG) - the one pair the
document calls out as commonly confused - do we look at a second page.
"""
from __future__ import annotations

from pathlib import Path

from app.llm.ollama_client import ask_vision, parse_json_response
from app.models.schemas import ClassificationResult
from app.utils.config_loader import load_form_types
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _build_classification_prompt() -> str:
    form_types = load_form_types()["form_types"]
    type_list = "\n".join(
        f"- {ft['code']}: {ft['name']} ({ft['purpose']}). Hints: {', '.join(ft['classification_hints'])}"
        for ft in form_types
    )
    return (
        "You are classifying a scanned BIFM Unit Trust form. Look at the form title, "
        "section headers, and any fund table on this page. Choose exactly ONE form code "
        "from the list below that best matches this page.\n\n"
        f"{type_list}\n\n"
        "Respond ONLY with a JSON object of the form:\n"
        '{"form_code": "<one of the codes above>", "confidence": <0-100 integer>, "reason": "<short reason>"}'
    )


def classify_form(first_page_image: Path) -> ClassificationResult:
    """Runs the primary classification call against page 1."""
    form_types_lookup = {ft["code"]: ft["name"] for ft in load_form_types()["form_types"]}
    prompt = _build_classification_prompt()

    response = ask_vision(prompt, first_page_image, json_mode=True)
    try:
        parsed = parse_json_response(response.text)
        code = parsed["form_code"]
        confidence = float(parsed.get("confidence", 0))
    except (KeyError, ValueError, Exception) as exc:  # noqa: BLE001
        logger.error("Could not parse classifier output for %s: %s | raw=%s", first_page_image, exc, response.text)
        code, confidence = "UNKNOWN", 0.0

    name = form_types_lookup.get(code, "Unknown / Unclassified")
    result = ClassificationResult(form_code=code, form_name=name, confidence=confidence, raw_model_output=response.text)
    logger.info("Classified %s as %s (%.0f%% confidence)", first_page_image.parent.name, code, confidence)
    return result


def disambiguate_gsg_vs_standard(fund_table_image: Path) -> ClassificationResult:
    """
    Section 3 / 7.2 edge case: GSG Disinvestment lists only the Global Sustainable
    Growth Fund; Standard Disinvestment can list multiple funds. Called only when
    the primary classification call returns one of these two codes with confidence
    below the 'high' threshold - i.e. not on every form, to avoid a wasted call.
    """
    prompt = (
        "This page is a Disinvestment form listing fund(s) for withdrawal. "
        "If the ONLY fund listed is 'BIFM Global Sustainable Growth Fund', this is the "
        "GSG Disinvestment form (code DIS_GSG). If multiple funds are listed, or a fund "
        "other than GSG, this is the Standard Disinvestment form (code DIS).\n\n"
        'Respond ONLY with JSON: {"form_code": "DIS_GSG" or "DIS", "confidence": <0-100>, "reason": "<short reason>"}'
    )
    response = ask_vision(prompt, fund_table_image, json_mode=True)
    try:
        parsed = parse_json_response(response.text)
        code = parsed["form_code"]
        confidence = float(parsed.get("confidence", 0))
    except Exception as exc:  # noqa: BLE001
        logger.error("GSG disambiguation parse failure: %s | raw=%s", exc, response.text)
        code, confidence = "DIS", 0.0

    name = "Disinvestment Form (GSG)" if code == "DIS_GSG" else "Disinvestment Form (Standard)"
    return ClassificationResult(form_code=code, form_name=name, confidence=confidence, raw_model_output=response.text)
