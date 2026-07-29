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

SHARED LEDGER DOCUMENTS: BIFM's real daily Cash and Trade Order batch
reports cover many portfolios/transactions in one file, and some
withdrawal letters instruct on several portfolios at once (all confirmed
against BIFM's actual sample documents - see ops/analyzer.py's TASK 3).
Correlation runs over "units" instead of documents directly: a normal
document contributes exactly one unit (itself, unchanged); a shared
ledger with N resolved OpsDocument.line_items contributes N units, one
per row, each carrying that row's own portfolio/amount/date/trade_id
instead of the document's (blank) top-level metadata. Units group
through the exact same trade-id / composite / unmatched logic as whole
documents always have - no special-casing needed, since a row unit
always has a portfolio+amount by construction (ops.analyzer drops any
row it couldn't resolve a portfolio for before it reaches
OpsDocument.line_items). Only at the end does a unit finally become an
OpsDocument: a single-row unit's document is used as-is (zero behaviour
change for the common case); a shared-ledger row's unit becomes a CLONE
of the source document with that row's metadata - same source_path, so
filing still copies the one real file, just once per transaction it's
actually evidence for, exactly as BIFM described needing ("Marvel will
go to all these five folders... and compile it"). A row that resolves
but matches nothing else in the batch isn't dropped - like any lone
document, it forms its own new transaction group, which is honest audit
evidence of a transaction Marvel doesn't yet have the rest of the paper
trail for, not something to hide.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime

from app.utils.logger import get_logger
from ops.models import OpsDocument, TransactionGroup
from ops.ops_config import load_workflow

logger = get_logger(__name__)


def _norm_trade_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _norm_amount(value: str) -> str:
    # Fixed-point, never scientific notation - this value is used both for
    # matching (any consistent representation works) and embedded directly
    # into the visible transaction key/folder name (composite grouping,
    # below), where ":g" turning a real BWP 13,392,853.26 amount into
    # "1.33929e+07" is a real defect, not just cosmetic.
    try:
        text = f"{float(value):.2f}".rstrip("0").rstrip(".")
        return text or "0"
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


@dataclass
class _Unit:
    """One correlatable row: a whole document (line_item=None, the common
    case) or one row of a shared-ledger document (line_item=that row's
    dict). Correlates on the row's own identifiers, not the document's."""
    doc: OpsDocument
    line_item: dict | None

    def _source(self) -> dict:
        return self.line_item if self.line_item is not None else self.doc.metadata

    def trade_id(self) -> str:
        return _norm_trade_id(self._source().get("trade_id", ""))

    def portfolio_code(self) -> str:
        return str(self._source().get("portfolio_code") or "")

    def amount(self) -> str:
        return _norm_amount(self._source().get("transaction_amount", ""))

    def date(self) -> str:
        return str(self._source().get("transaction_date") or "")

    def materialize(self) -> OpsDocument:
        """The whole-document unit IS the document (no copy - preserves
        every existing single-document behaviour unchanged). A row unit
        becomes a clone: same physical file (source_path/source_file,
        classification, everything else), but this row's own
        portfolio/amount/date/trade_id instead of the shared document's
        blank top-level metadata.

        No review flag is raised for this split itself - splitting a
        shared document across the transactions it's evidence for is
        correct, expected behaviour (exactly what BIFM described: "Marvel
        will go to all these five folders... and compile it"), not
        something for a human to double-check. review_flags is reserved
        for things that actually need attention (an unresolved portfolio,
        a missing trade id/amount); a purely informational note here would
        make every correctly-split row show up as "needs review" for no
        real reason."""
        if self.line_item is None:
            return self.doc
        clone = copy.deepcopy(self.doc)
        clone.metadata["portfolio_code"] = self.line_item.get("portfolio_code", "")
        clone.metadata["portfolio_name"] = self.line_item.get("portfolio_name", "")
        clone.metadata["trade_id"] = self.line_item.get("trade_id", "")
        clone.metadata["transaction_amount"] = self.line_item.get("transaction_amount", "")
        if self.line_item.get("transaction_date"):
            clone.metadata["transaction_date"] = self.line_item["transaction_date"]
        clone.review_flags = [
            f for f in clone.review_flags
            if "row(s) of this shared document" not in f
        ]
        return clone


def _units_for(doc: OpsDocument) -> list[_Unit]:
    if doc.line_items:
        return [_Unit(doc, item) for item in doc.line_items]
    return [_Unit(doc, None)]


def correlate(documents: list[OpsDocument]) -> list[TransactionGroup]:
    """Groups analysed documents into transactions. Sets each resulting
    document's transaction_key in place and returns the groups. The
    returned groups' documents may include clones of a shared-ledger
    document (see module docstring) - callers should file/save those
    materialized documents, not the original `documents` list, so a
    shared document gets filed once per transaction it actually belongs
    to."""
    tolerance = int(load_workflow().get("date_tolerance_days", 5))

    units: list[_Unit] = [u for doc in documents for u in _units_for(doc)]

    trade_groups: dict[str, list[_Unit]] = {}
    composite_units: list[_Unit] = []
    unmatched_units: list[_Unit] = []

    for u in units:
        trade_id = u.trade_id()
        if trade_id:
            trade_groups.setdefault(trade_id, []).append(u)
        elif u.portfolio_code() and u.amount():
            composite_units.append(u)
        else:
            unmatched_units.append(u)

    # Absorb composite units into an existing trade-id group when they
    # share its portfolio + amount profile and sit within the date window -
    # the bank statement rarely quotes the trade id, but it shows the same
    # amount moving on the same portfolio in the same week.
    leftovers: list[_Unit] = []
    for u in composite_units:
        placed = False
        profile = (u.portfolio_code(), u.amount())
        for members in trade_groups.values():
            member_profiles = {(m.portfolio_code(), m.amount()) for m in members}
            if profile in member_profiles and all(
                _dates_within(u.date(), m.date(), tolerance) for m in members
            ):
                members.append(u)
                placed = True
                break
        if not placed:
            leftovers.append(u)

    # Composite grouping among the leftovers: portfolio + amount + date window.
    composite_groups: list[list[_Unit]] = []
    for u in leftovers:
        profile = (u.portfolio_code(), u.amount())
        placed = False
        for group in composite_groups:
            group_profile = (group[0].portfolio_code(), group[0].amount())
            if profile == group_profile and all(
                _dates_within(u.date(), m.date(), tolerance) for m in group
            ):
                group.append(u)
                placed = True
                break
        if not placed:
            composite_groups.append([u])

    groups: list[TransactionGroup] = []

    def _build(key: str, unit_members: list[_Unit]) -> TransactionGroup:
        materialized = [u.materialize() for u in unit_members]
        for m in materialized:
            m.transaction_key = key
        return TransactionGroup(
            transaction_key=key,
            transaction_type=_infer_transaction_type(materialized),
            portfolio_code=_first_nonempty(materialized, "portfolio_code"),
            portfolio_name=_first_nonempty(materialized, "portfolio_name"),
            client_name=_first_nonempty(materialized, "client_name"),
            transaction_date=_first_nonempty(materialized, "transaction_date"),
            transaction_amount=_first_nonempty(materialized, "transaction_amount"),
            trade_id=_first_nonempty(materialized, "trade_id"),
            salesperson=_first_nonempty(materialized, "salesperson"),
            documents=materialized,
        )

    for trade_id, members in trade_groups.items():
        groups.append(_build(trade_id, members))

    for group in composite_groups:
        code = group[0].portfolio_code()
        amount = group[0].amount()
        date = next((u.date() for u in group if u.date()), "") or "undated"
        groups.append(_build(f"{code}-{amount}-{date}", group))

    # Unmatched: a unit with no trade id, portfolio, or amount at all can
    # only ever be a whole-document unit - a shared-ledger row unit always
    # has both by construction (ops.analyzer drops any row it couldn't
    # resolve a portfolio for before it ever reaches OpsDocument.line_items,
    # so an unresolvable-everywhere shared document degrades to a normal
    # single whole-document unit here, same flagged treatment as any other
    # uncorrelatable document). A row that resolves but matches nothing
    # else in this batch isn't silently dropped either - it forms its own
    # new composite group above, exactly like a lone document always has;
    # that's honest audit evidence of a transaction Marvel doesn't yet
    # have the rest of the paper trail for, not something to hide.
    for i, u in enumerate(unmatched_units, start=1):
        u.doc.review_flags.append("Could not be correlated to a transaction (no Trade ID, portfolio, or amount)")
        groups.append(_build(f"UNMATCHED-{i:03d}", [u]))

    logger.info(
        "Correlated %d document(s) into %d transaction(s) (%d unmatched)",
        len(documents), len(groups), len(unmatched_units),
    )
    return groups
