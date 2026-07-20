"""
Use Case 2 document analysis: classification + metadata extraction in ONE
LLM call per document.

Covers two proposal steps at once:
  - "Classify incoming documents (e.g., Withdrawal Instruction, Deposit
    Confirmation Letter, Proof of Payment)"
  - "Intelligently extract, validate, and maintain the metadata below..."
    (the full metadata table: Portfolio Code/Name, Client Name,
    Transaction/Instruction Type, dates, amount, security, Trade ID,
    document type/source)

One combined call instead of separate classify + extract calls halves the
API cost per document; the two questions share all their context anyway
(the same page/text answers both).

PDFs go through Use Case 1's rendering stack (app.ocr.pdf_utils - deskew,
enhancement) and a vision call; email bodies / text documents go through a
text call. Deterministic post-processing then resolves the portfolio via
the client mapping, normalises dates/amounts, and raises review flags for
the human-in-the-loop queue ("Low-confidence extractions, discrepancies,
and incomplete document sets are flagged for user review").
"""
from __future__ import annotations

import re
from datetime import datetime

from app.llm.router import ask_text, ask_vision, parse_json_response
from app.ocr.pdf_utils import render_pdf_to_images
from app.utils.confidence import cap_confidence
from app.utils.logger import get_logger
from ops.intake import IntakeItem
from ops.models import OpsDocument
from ops.ops_config import load_document_types, load_workflow
from ops.portfolio import resolve_portfolio

logger = get_logger(__name__)

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%d.%m.%Y"]

# Metadata the LLM is asked for (portfolio is asked as free text and
# resolved to a code afterwards via the client mapping).
_LLM_FIELDS = [
    ("portfolio", "Portfolio name or portfolio code the transaction concerns"),
    ("client_name", "Client / portfolio holder name"),
    ("transaction_type", "Withdrawal or Contribution"),
    ("transaction_date", "Instruction date on the document"),
    ("trade_date", "Trade execution date, if stated"),
    ("transaction_amount", "The withdrawal or contribution amount (number)"),
    ("security_name", "Security / fund name involved, if stated"),
    ("security_code", "Security code / ISIN / instrument code, if stated"),
    ("trade_id", "Trade ID / transaction reference / order number, if stated"),
]


def _build_prompt(is_text: bool) -> str:
    doc_types = load_document_types()
    type_list = "\n".join(
        f"- {d['code']}: {d['name']}. Hints: {', '.join(d['classification_hints'])}"
        for d in doc_types
    )
    field_list = "\n".join(f'- "{fid}": {desc}' for fid, desc in _LLM_FIELDS)
    medium = "email/text document" if is_text else "scanned document page"
    return (
        f"You are analysing a {medium} for BIFM's Operations team, which "
        "processes client Withdrawal and Contribution transactions.\n\n"
        "TASK 1 - classify the document as exactly ONE of:\n"
        f"{type_list}\n"
        "If it is none of these, use code UNRECOGNIZED.\n\n"
        "TASK 2 - extract this metadata (null for anything not present; "
        "never invent values):\n"
        f"{field_list}\n\n"
        "Respond ONLY with this JSON shape:\n"
        "{\n"
        '  "doc_type": "<code>", "confidence": <0-100>,\n'
        '  "metadata": {"<field>": {"value": <value or null>, "confidence": <0-100>}, ...}\n'
        "}"
    )


def _normalize_date(value) -> str:
    if not value:
        return ""
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text  # keep what was read; the report shows it verbatim


def _normalize_amount(value) -> str:
    if value in (None, ""):
        return ""
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        number = float(cleaned)
    except ValueError:
        return str(value)
    return f"{number:g}"


def analyze_item(item: IntakeItem) -> OpsDocument:
    """One intake item -> classified, metadata-tagged OpsDocument."""
    doc = OpsDocument(source_file=item.path.name, source_path=str(item.path))
    prompt = _build_prompt(is_text=item.kind == "text")

    try:
        if item.kind == "text":
            text = item.body_text or item.path.read_text(encoding="utf-8", errors="replace")
            response = ask_text(f"{prompt}\n\nDOCUMENT TEXT:\n{text[:8000]}", json_mode=True)
        else:
            pages = render_pdf_to_images(item.path, output_subdir=f"ops_{item.path.stem}", page_numbers=[1])
            response = ask_vision(prompt, pages[1], json_mode=True)
        parsed = parse_json_response(response.text)
    except Exception as exc:  # noqa: BLE001 - one bad document must not stop the batch
        logger.exception("Ops analysis failed for %s", item.path.name)
        doc.error = str(exc)
        doc.review_flags.append(f"Analysis failed: {exc}")
        return doc

    valid_codes = {d["code"] for d in load_document_types()}
    code = str(parsed.get("doc_type", "UNRECOGNIZED"))
    if code not in valid_codes:
        code = "UNRECOGNIZED"
    doc.doc_type_code = code
    doc.doc_type_name = next(
        (d["name"] for d in load_document_types() if d["code"] == code), "Unrecognized document")
    doc.classification_confidence = cap_confidence(parsed.get("confidence", 0))

    raw_meta = parsed.get("metadata", {}) or {}

    def read(field_id: str) -> tuple[str, float]:
        payload = raw_meta.get(field_id)
        if payload is None:
            return "", 0.0
        if isinstance(payload, dict):
            value = payload.get("value")
            conf = cap_confidence(payload.get("confidence", 0))
        else:
            value, conf = payload, 50.0
        return ("" if value is None else str(value).strip()), conf

    portfolio_raw, portfolio_conf = read("portfolio")
    code_resolved, name_resolved = resolve_portfolio(portfolio_raw)
    doc.metadata["portfolio_code"] = code_resolved
    doc.metadata["portfolio_name"] = name_resolved or portfolio_raw
    doc.metadata_confidence["portfolio_code"] = portfolio_conf if code_resolved else 0.0

    for field_id, _desc in _LLM_FIELDS[1:]:
        value, conf = read(field_id)
        if field_id in ("transaction_date", "trade_date"):
            value = _normalize_date(value)
        elif field_id == "transaction_amount":
            value = _normalize_amount(value)
        elif field_id == "transaction_type":
            low = value.lower()
            value = "Withdrawal" if "withdraw" in low else ("Contribution" if "contribut" in low or "deposit" in low else value)
        doc.metadata[field_id] = value
        doc.metadata_confidence[field_id] = conf

    # Fields known from intake context, not the LLM.
    doc.metadata["instruction_type"] = doc.doc_type_name
    doc.metadata["email_date"] = item.email_date
    doc.metadata["document_type"] = "Email" if item.kind == "text" else "PDF"
    doc.metadata["source_document"] = item.source_label

    # Transaction type can be implied by the document type when the model
    # didn't state it (a Withdrawal Instruction IS a withdrawal document).
    if not doc.metadata.get("transaction_type"):
        implied = next(
            (d.get("transaction_type", "Any") for d in load_document_types() if d["code"] == code), "Any")
        if implied in ("Withdrawal", "Contribution"):
            doc.metadata["transaction_type"] = implied

    # Human-in-the-loop flags.
    threshold = float(load_workflow().get("review_confidence_threshold", 65))
    if doc.classification_confidence < threshold:
        doc.review_flags.append(
            f"Low classification confidence ({doc.classification_confidence:.0f}%)")
    if portfolio_raw and not code_resolved:
        doc.review_flags.append(
            f"Portfolio '{portfolio_raw}' not found in the Portfolio Name-to-Code mapping")
    if not portfolio_raw:
        doc.review_flags.append("No portfolio stated on the document")
    if not doc.metadata.get("trade_id") and not doc.metadata.get("transaction_amount"):
        doc.review_flags.append("No Trade ID and no amount - correlation will need manual review")

    return doc
