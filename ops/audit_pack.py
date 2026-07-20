"""
Audit pack evaluation + compilation for Use Case 2.

"Withdrawals: Auto-compile the Client Withdrawal Instruction, Investment
Team Approval / Instruction Email, Trade Order Document (Trading Tool),
Cash Flow Statement, and Bank Statement / Proof of Payment (where
applicable). Contributions: Auto-compile the Client Proof of Payment /
Deposit Confirmation Letter, Bank Statement confirming receipt, Proof of
Transfer from the BIFM account to the investment inflow account, Cash
Template, and Trade Template (where applicable based on client type).
Consolidate all documents per transaction into a centralized audit folder
organized by Portfolio Code and Transaction Identifier, with configurable
folder naming conventions (Portfolio Code, Transaction ID, Transaction
Date) - enabling rapid retrieval of complete audit evidence."

Pack composition lives in ops/config/ops_document_types.json; the folder
naming convention lives in ops_workflow.json ("audit_folder_name", with
{portfolio_code} / {transaction_id} / {transaction_date} placeholders).
Each compiled pack gets a MANIFEST.txt listing what's present, what
satisfied each requirement, and exactly what's missing - so an auditor
opening the folder sees the pack's completeness at a glance without
opening the app.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from app.utils.logger import get_logger
from ops.models import AuditPack, PackItem, TransactionGroup
from ops.ops_config import (
    OPS_AUDIT_DIR,
    document_type_lookup,
    ensure_output_dirs,
    load_audit_pack_definitions,
    load_workflow,
)

logger = get_logger(__name__)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._\- ]")


def _safe(name: str) -> str:
    return _SAFE_RE.sub("_", str(name)).strip() or "UNSPECIFIED"


def evaluate_pack(transaction: TransactionGroup) -> AuditPack:
    """Checks the transaction's documents against the configured pack
    composition for its type. Transactions of unknown type get an empty
    item list - there's nothing defined to check them against."""
    definitions = load_audit_pack_definitions().get(transaction.transaction_type, [])
    names = document_type_lookup()
    present_codes = transaction.doc_type_codes

    items: list[PackItem] = []
    for entry in definitions:
        accepted = entry.get("satisfied_by", [entry["code"]])
        satisfied = next((c for c in accepted if c in present_codes), "")
        items.append(PackItem(
            code=entry["code"],
            name=entry.get("note") or names.get(entry["code"], entry["code"]),
            required=bool(entry.get("required", True)),
            present=bool(satisfied),
            satisfied_by_code=satisfied,
            note=entry.get("note", ""),
        ))
    return AuditPack(transaction=transaction, items=items)


def compile_pack(pack: AuditPack) -> Path:
    """Copies every document of the transaction into the centralized audit
    repository folder and writes the MANIFEST.txt. Returns the folder."""
    ensure_output_dirs()
    tx = pack.transaction
    folder_pattern = load_workflow().get("audit_folder_name", "{portfolio_code}_{transaction_id}_{transaction_date}")
    folder_name = _safe(folder_pattern.format(
        portfolio_code=tx.portfolio_code or "NOPORTFOLIO",
        transaction_id=tx.trade_id or tx.transaction_key,
        transaction_date=tx.transaction_date or "undated",
    ))
    dest_dir = OPS_AUDIT_DIR / _safe(tx.portfolio_code or "NOPORTFOLIO") / folder_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    for doc in tx.documents:
        source = Path(doc.filed_path or doc.source_path)
        if not source.exists():
            continue
        dest = dest_dir / f"{doc.doc_type_code}_{source.name}"
        if not dest.exists():
            shutil.copy2(source, dest)

    manifest_lines = [
        f"AUDIT PACK - {tx.transaction_type} transaction",
        f"Portfolio:        {tx.portfolio_code} {tx.portfolio_name}".rstrip(),
        f"Client:           {tx.client_name}",
        f"Transaction key:  {tx.transaction_key}",
        f"Trade ID:         {tx.trade_id}",
        f"Transaction date: {tx.transaction_date}",
        f"Amount:           {tx.transaction_amount}",
        f"Status:           {pack.status}",
        "",
        "Pack contents:",
    ]
    for item in pack.items:
        mark = "[x]" if item.present else ("[MISSING]" if item.required else "[not provided - optional]")
        via = f" (satisfied by {item.satisfied_by_code})" if item.satisfied_by_code and item.satisfied_by_code != item.code else ""
        manifest_lines.append(f"  {mark} {item.name}{via}")
    manifest_lines += ["", "Documents:"]
    for doc in tx.documents:
        manifest_lines.append(f"  - {doc.doc_type_name}: {Path(doc.source_path).name}")

    (dest_dir / "MANIFEST.txt").write_text("\n".join(manifest_lines), encoding="utf-8")
    pack.audit_folder = str(dest_dir)
    logger.info("Compiled audit pack %s (%s)", dest_dir.name, pack.status)
    return dest_dir
