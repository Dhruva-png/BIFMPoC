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
the client mapping, resolves the salesperson (extracted from the document
if it states one, else DEMO-assigned via the portfolio - see
ops/salesperson.py), normalises dates/amounts, and raises review flags for
the human-in-the-loop queue ("Low-confidence extractions, discrepancies,
and incomplete document sets are flagged for user review").

A third, optional extraction task handles BIFM's real SHARED LEDGER
documents - the daily Cash and Trade Order batch reports, and
multi-portfolio withdrawal letters, all confirmed against BIFM's actual
sample documents - which cover several portfolios/transactions in one
file rather than the usual one-document-one-transaction case. Each
resolved row is stored on OpsDocument.line_items; ops/correlator.py
attaches a per-transaction copy of the same physical document to every
transaction one of its rows actually matches.
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
from ops.salesperson import resolve_salesperson

logger = get_logger(__name__)

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%d.%m.%Y"]

# Metadata the LLM is asked for (portfolio is asked as free text and
# resolved to a code afterwards via the client mapping).
_LLM_FIELDS = [
    ("portfolio", "Portfolio/fund NAME the transaction concerns (e.g. 'BIFM Pula Money Market Fund'). "
     "Prefer the written fund/portfolio name over a raw 'Fund Number' or account-reference column next "
     "to it on the same form - that number is the client form's own internal reference, not one of "
     "BIFM's portfolio codes, and will never match the Portfolio Name-to-Code mapping"),
    ("client_name", "Client / portfolio holder name"),
    ("transaction_type", "Withdrawal or Contribution"),
    ("transaction_date", "Instruction date on the document"),
    ("trade_date", "Trade execution date, if stated"),
    ("transaction_amount", "The withdrawal or contribution amount (number)"),
    ("security_name", "Security / fund name involved, if stated"),
    ("security_code", "Security code / ISIN / instrument code, if stated"),
    ("trade_id", "Trade ID / transaction reference / order number, if stated"),
    ("salesperson", "Sales consultant / advisor / broker name mentioned on the document, if any"),
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
        "TASK 3 - some BIFM documents genuinely cover SEVERAL SEPARATE "
        "PORTFOLIO-LEVEL TRANSACTIONS in one file: either an operational "
        "ledger whose rows are different clients' own withdrawals/"
        "contributions/orders (e.g. a daily cash movement report, or a "
        "trade order batch), or a single letter/email in which ONE client "
        "instructs on SEVERAL of their own portfolios at once (e.g. a "
        "table listing 2+ portfolio codes, each with its own amount). "
        "This is different from a normal single-transaction document, "
        "and different from a fund's investment/holdings schedule, which "
        "lists many underlying securities/bonds as supporting detail but "
        "is still evidence for only ONE portfolio's transaction - use "
        "TASK 2 for that, not this. Only if THIS SPECIFIC document "
        'actually covers multiple portfolios, list each one as '
        '"line_items": one entry per portfolio/transaction, each with '
        '"portfolio" (the fund/portfolio NAME, preferred over a raw Fund '
        'Number/account-reference column), "transaction_amount", '
        '"transaction_date" (that row\'s own stated transaction/value '
        "date - never an unrelated per-instrument date like a settlement, "
        'maturity, or order-validity date), and "trade_id" - ONLY if it '
        "looks like a reference likely to also appear on OTHER documents "
        "for the same transaction (e.g. a bank payment reference); leave "
        "it null for a purely internal per-row ledger ID that exists "
        "only in this table, since using that would stop this row from "
        "ever matching those other documents. List at most 15 rows (the "
        "ones with the clearest portfolio identifiers); if there are "
        "more, this is almost certainly not what TASK 3 is for after all "
        '- leave "line_items" empty and use TASK 2\'s single-transaction '
        "fields instead, which is also correct for "
        "the common case of a normal document.\n\n"
        "Respond ONLY with this JSON shape:\n"
        "{\n"
        '  "doc_type": "<code>", "confidence": <0-100>,\n'
        '  "metadata": {"<field>": {"value": <value or null>, "confidence": <0-100>}, ...},\n'
        '  "line_items": [{"portfolio": <value or null>, "transaction_amount": <value or null>, '
        '"transaction_date": <value or null>, "trade_id": <value or null>}, ...]\n'
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
    # Fixed-point, never scientific notation - ":g" switches to exponential
    # past 6 significant digits (e.g. a real BWP 13,392,853.26 withdrawal
    # became the transaction key "LIBGLO-1.33929e+07-..." during a live
    # test against BIFM's actual sample documents). Trailing zeros/dot are
    # still stripped so whole amounts stay clean ("25000" not "25000.00").
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _field_value(meta: dict, field_id: str):
    payload = meta.get(field_id)
    return payload.get("value") if isinstance(payload, dict) else payload


def _is_weak(parsed: dict) -> bool:
    """True when a read found nothing usable - either it couldn't classify
    the document at all, or classified it but read none of the core
    transaction fields. Some real BIFM documents (e.g. BIFM UT's own
    Additional Investment Form) put every cover/instructions paragraph on
    page 1 and the actual investor/fund/amount table on page 2 - a
    page-1-only read of one of these comes back exactly this way."""
    if str(parsed.get("doc_type", "UNRECOGNIZED")) == "UNRECOGNIZED":
        return True
    meta = parsed.get("metadata", {}) or {}
    return not any(_field_value(meta, f) for f in ("portfolio", "client_name", "transaction_amount", "trade_id"))


def analyze_item(item: IntakeItem, batch_fallback_salesperson: str | None = None) -> OpsDocument:
    """One intake item -> classified, metadata-tagged OpsDocument.

    batch_fallback_salesperson: the one salesperson name ops.pipeline
    picked at random for this whole batch (see its docstring) - passed
    through to ops.salesperson.resolve_salesperson so that every document
    in one intake run which doesn't state or resolve a real salesperson
    still ends up attributed to the SAME name, not each independently
    rolling its own. None when analyze_item is called standalone (e.g.
    directly, outside a batch), in which case resolve_salesperson falls
    back to its own per-call random pick."""
    doc = OpsDocument(source_file=item.path.name, source_path=str(item.path))
    prompt = _build_prompt(is_text=item.kind == "text")

    try:
        if item.kind == "text":
            text = item.body_text or item.path.read_text(encoding="utf-8", errors="replace")
            response = ask_text(f"{prompt}\n\nDOCUMENT TEXT:\n{text[:8000]}", json_mode=True)
            parsed = parse_json_response(response.text)
        else:
            pages = render_pdf_to_images(item.path, output_subdir=f"ops_{item.path.stem}", page_numbers=[1])
            response = ask_vision(prompt, pages[1], json_mode=True)
            parsed = parse_json_response(response.text)
            if _is_weak(parsed):
                # Page 1 alone read as unrecognized/empty - try page 2
                # before giving up. Cheap since this only fires for
                # documents that already looked unusable from page 1.
                # render_pdf_to_images raises when a requested page simply
                # doesn't exist (e.g. a genuinely one-page document, like a
                # bare KYC scan) - that's an expected outcome here, not a
                # failure, so it's swallowed rather than aborting the whole
                # document's analysis over an optional secondary attempt.
                try:
                    more_pages = render_pdf_to_images(
                        item.path, output_subdir=f"ops_{item.path.stem}", page_numbers=[2])
                except Exception:  # noqa: BLE001
                    more_pages = {}
                if 2 in more_pages:
                    response2 = ask_vision(prompt, more_pages[2], json_mode=True)
                    parsed2 = parse_json_response(response2.text)
                    if not _is_weak(parsed2):
                        parsed = parsed2
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

    # Portfolio fallback: some real BIFM UT retail forms print a client-
    # facing "Fund Number"/account-reference column right next to the fund
    # name in the same table, and the model sometimes reads that number as
    # "portfolio" even though it's the form's own internal reference, not
    # one of BIFM's portfolio codes - it will never resolve. security_name
    # is read from the same table and usually names the fund correctly;
    # retry resolution against that before giving up.
    if not code_resolved and doc.metadata.get("security_name"):
        fallback_code, fallback_name = resolve_portfolio(doc.metadata["security_name"])
        if fallback_code:
            code_resolved, name_resolved = fallback_code, fallback_name
            doc.metadata["portfolio_code"] = code_resolved
            doc.metadata["portfolio_name"] = name_resolved
            doc.metadata_confidence["portfolio_code"] = doc.metadata_confidence.get("security_name", 0.0)

    # Salesperson: prefer whatever the document itself stated (just read
    # generically above, in doc.metadata["salesperson"]); fall back to the
    # DEMO roster keyed by the now-resolved portfolio code, or to this
    # batch's single random pick. See ops/salesperson.py - this whole
    # field is a demo capability standing in for a future SharePoint/HR-
    # directory integration.
    resolved_name, source = resolve_salesperson(
        doc.metadata.get("salesperson"), code_resolved, batch_fallback=batch_fallback_salesperson)
    doc.metadata["salesperson"] = resolved_name
    doc.salesperson_source = source
    if source == "Assigned (demo roster)":
        doc.metadata_confidence["salesperson"] = 0.0  # a lookup, not a model read

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

    # TASK 3: shared-ledger rows (BIFM's real daily Cash and Trade Order
    # batch reports, and multi-portfolio withdrawal letters, cover several
    # portfolios/transactions in one document - see this module's
    # docstring and ops/correlator.py). Each resolved row lets the
    # correlator attach a per-transaction copy of this same physical
    # document to every transaction it's actually evidence for, instead of
    # this one document only ever being able to belong to a single
    # transaction. Rows whose portfolio can't be resolved are dropped
    # (they can never correlate anyway) but counted, so a human still
    # knows this ledger has other rows not yet accounted for.
    raw_line_items = parsed.get("line_items")
    unresolved_rows = 0
    if isinstance(raw_line_items, list):
        # Hard cap regardless of what the model returned - the prompt asks
        # for at most 15, but a document it misjudged as a multi-client
        # ledger (e.g. a fund's holdings schedule, dozens of securities)
        # shouldn't be allowed to balloon processing here even if it
        # ignored that instruction.
        for raw_item in raw_line_items[:20]:
            if not isinstance(raw_item, dict):
                continue
            item_portfolio_raw = str(raw_item.get("portfolio") or "").strip()
            item_code, item_name = resolve_portfolio(item_portfolio_raw)
            item_amount = _normalize_amount(raw_item.get("transaction_amount"))
            item_date = _normalize_date(raw_item.get("transaction_date")) or doc.metadata.get("transaction_date", "")
            item_trade_id = str(raw_item.get("trade_id") or "").strip()
            if not item_code or not item_amount:
                unresolved_rows += 1
                continue
            doc.line_items.append({
                "portfolio_code": item_code,
                "portfolio_name": item_name,
                "transaction_amount": item_amount,
                "transaction_date": item_date,
                "trade_id": item_trade_id,
            })
    if unresolved_rows:
        doc.review_flags.append(
            f"{unresolved_rows} row(s) of this shared document couldn't be resolved to a known "
            "portfolio and were skipped - extend the Portfolio Name-to-Code mapping to pick them up")

    # Human-in-the-loop flags. Skipped for the portfolio/trade-id/amount
    # checks when line_items resolved something - a shared ledger's own
    # top-level metadata is expected to be blank (there's no single
    # "the" portfolio for a document covering several), so those flags
    # would just be noise once at least one row correlated successfully.
    threshold = float(load_workflow().get("review_confidence_threshold", 65))
    if doc.classification_confidence < threshold:
        doc.review_flags.append(
            f"Low classification confidence ({doc.classification_confidence:.0f}%)")
    if not doc.line_items:
        if portfolio_raw and not code_resolved:
            doc.review_flags.append(
                f"Portfolio '{portfolio_raw}' not found in the Portfolio Name-to-Code mapping")
        if not portfolio_raw:
            doc.review_flags.append("No portfolio stated on the document")
        if not doc.metadata.get("trade_id") and not doc.metadata.get("transaction_amount"):
            doc.review_flags.append("No Trade ID and no amount - correlation will need manual review")

    return doc
