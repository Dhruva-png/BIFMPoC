"""
Cross-document transaction correlation for Use Case 2.

"Correlate documents belonging to the same transaction across multiple
folders and repositories using configurable identifiers: Portfolio Code /
Name, Client Name, Transaction Date, Trade ID / Transaction Reference,
Transaction Amount, and Security Name / Code."

Fully deterministic - no LLM. Correlation keys, in priority order:

  1. Trade ID / Transaction Reference (normalised). The strongest
     identifier: two documents citing the same trade reference ARE the
     same transaction, whatever else differs.
  2. Composite: portfolio code + amount, with transaction dates within
     the configured tolerance (documents for one transaction are dated
     across a few days - instruction first, trade order and bank
     statement later). Documents without a date still join a composite
     group on portfolio + amount alone.
  3. Neither -> a singleton "unmatched" group flagged for manual review,
     never force-merged into someone else's transaction.

A trade-id group also absorbs composite-keyed documents that share its
portfolio + amount profile (the bank statement usually doesn't quote the
trade id, but it does show the amount against the portfolio's account).
"""
from __future__ import annotations

import re
from datetime import datetime

from app.utils.logger import get_logger
from ops.models import OpsDocument, TransactionGroup
from ops.ops_config import load_workflow

logger = get_logger(__name__)


def _norm_trade_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _norm_amount(value: str) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _dates_within(a: str, b: str, tolerance_days: int) -> bool:
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return True  # a missing date never blocks a portfolio+amount match
    return abs((da - db).days) <= tolerance_days


def _first_nonempty(documents: list[OpsDocument], field_id: str) -> str:
    for doc in documents:
        value = doc.meta(field_id)
        if value:
            return value
    return ""


def _infer_transaction_type(documents: list[OpsDocument]) -> str:
    """The instruction document type is authoritative (a Withdrawal
    Instruction IS a withdrawal); metadata majority breaks ties."""
    codes = {d.doc_type_code for d in documents}
    if "WITHDRAWAL_INSTRUCTION" in codes:
        return "Withdrawal"
    if codes & {"DEPOSIT_CONFIRMATION", "PROOF_OF_TRANSFER", "CASH_TEMPLATE", "TRADE_TEMPLATE"}:
        return "Contribution"
    votes = [d.meta("transaction_type") for d in documents if d.meta("transaction_type")]
    if votes:
        return max(set(votes), key=votes.count)
    return "Unknown"


def correlate(documents: list[OpsDocument]) -> list[TransactionGroup]:
    """Groups analysed documents into transactions. Sets each document's
    transaction_key in place and returns the groups."""
    tolerance = int(load_workflow().get("date_tolerance_days", 5))

    trade_groups: dict[str, list[OpsDocument]] = {}
    composite_docs: list[OpsDocument] = []
    unmatched: list[OpsDocument] = []

    for doc in documents:
        trade_id = _norm_trade_id(doc.meta("trade_id"))
        if trade_id:
            trade_groups.setdefault(trade_id, []).append(doc)
        elif doc.meta("portfolio_code") and _norm_amount(doc.meta("transaction_amount")):
            composite_docs.append(doc)
        else:
            unmatched.append(doc)

    # Absorb composite documents into an existing trade-id group when they
    # share its portfolio + amount profile and sit within the date window -
    # the bank statement rarely quotes the trade id, but it shows the same
    # amount moving on the same portfolio in the same week.
    leftovers: list[OpsDocument] = []
    for doc in composite_docs:
        placed = False
        profile = (doc.meta("portfolio_code"), _norm_amount(doc.meta("transaction_amount")))
        for members in trade_groups.values():
            member_profiles = {
                (m.meta("portfolio_code"), _norm_amount(m.meta("transaction_amount")))
                for m in members
            }
            if profile in member_profiles and all(
                _dates_within(doc.meta("transaction_date"), m.meta("transaction_date"), tolerance)
                for m in members
            ):
                members.append(doc)
                placed = True
                break
        if not placed:
            leftovers.append(doc)

    # Composite grouping among the leftovers: portfolio + amount + date window.
    composite_groups: list[list[OpsDocument]] = []
    for doc in leftovers:
        profile = (doc.meta("portfolio_code"), _norm_amount(doc.meta("transaction_amount")))
        placed = False
        for group in composite_groups:
            group_profile = (group[0].meta("portfolio_code"),
                             _norm_amount(group[0].meta("transaction_amount")))
            if profile == group_profile and all(
                _dates_within(doc.meta("transaction_date"), m.meta("transaction_date"), tolerance)
                for m in group
            ):
                group.append(doc)
                placed = True
                break
        if not placed:
            composite_groups.append([doc])

    groups: list[TransactionGroup] = []

    def _build(key: str, members: list[OpsDocument]) -> TransactionGroup:
        for m in members:
            m.transaction_key = key
        return TransactionGroup(
            transaction_key=key,
            transaction_type=_infer_transaction_type(members),
            portfolio_code=_first_nonempty(members, "portfolio_code"),
            portfolio_name=_first_nonempty(members, "portfolio_name"),
            client_name=_first_nonempty(members, "client_name"),
            transaction_date=_first_nonempty(members, "transaction_date"),
            transaction_amount=_first_nonempty(members, "transaction_amount"),
            trade_id=_first_nonempty(members, "trade_id"),
            documents=members,
        )

    for trade_id, members in trade_groups.items():
        groups.append(_build(trade_id, members))

    for group in composite_groups:
        code = group[0].meta("portfolio_code")
        amount = _norm_amount(group[0].meta("transaction_amount"))
        date = _first_nonempty(group, "transaction_date") or "undated"
        groups.append(_build(f"{code}-{amount}-{date}", group))

    for i, doc in enumerate(unmatched, start=1):
        doc.review_flags.append("Could not be correlated to a transaction (no Trade ID, portfolio, or amount)")
        groups.append(_build(f"UNMATCHED-{i:03d}", [doc]))

    logger.info(
        "Correlated %d document(s) into %d transaction(s) (%d unmatched)",
        len(documents), len(groups), len(unmatched),
    )
    return groups
