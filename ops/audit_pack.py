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

When settings.sharepoint.ops_write_back_enabled is set, each compiled
pack folder (documents + MANIFEST.txt) is also pushed to SharePoint under
settings.sharepoint.ops_audit_folder, at the same PortfolioCode/... path
computed locally - this is the actual audit-evidence deliverable the
proposal describes ("enabling rapid retrieval of complete audit evidence
for internal and external audit requests"), so it's the one most worth
having live on a real SharePoint site.
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

from app.utils.logger import get_logger
from config.settings import settings
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
        f"Salesperson:      {tx.salesperson}",
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

    if settings.sharepoint.ops_write_back_enabled:
        from app.connectors import sharepoint_client
        remote_folder = f"{settings.sharepoint.ops_audit_folder}/{dest_dir.relative_to(OPS_AUDIT_DIR)}".replace("\\", "/")
        for local_file in dest_dir.iterdir():
            if local_file.is_file():
                sharepoint_client.upload_file(local_file, remote_folder)

    return dest_dir


def zip_audit_folders(folder_paths: list[str]) -> bytes:
    """
    Bundles a list of already-compiled audit pack folders (each holding
    its documents + MANIFEST.txt) into a single in-memory zip. Folders
    that are blank or missing on disk are silently skipped rather than
    raising - a stale reference should degrade to "not included", not
    break the whole export. Takes plain folder path strings (not AuditPack
    objects) so both the UI (in-memory packs from the run just completed)
    and the CLI (folder paths read back from the repository DB, which
    outlives any single run's in-memory objects) can share this.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder_str in folder_paths:
            if not folder_str:
                continue
            folder = Path(folder_str)
            if not folder.exists():
                continue
            for file_path in folder.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, arcname=f"{folder.name}/{file_path.relative_to(folder)}")
    return buffer.getvalue()


def zip_packs_for_salesperson(packs: list[AuditPack], salesperson: str) -> bytes:
    """The "create audit packs based on the salesperson's name" capability,
    from in-memory AuditPack objects (e.g. right after a UI run). Each
    transaction's compiled pack folder is unchanged - audit packs are
    fundamentally per-transaction, per BIFM's own requirement; this is a
    filtered EXPORT on top, not a restructuring of how packs are built."""
    folders = [p.audit_folder for p in packs if p.transaction.salesperson == salesperson]
    return zip_audit_folders(folders)
