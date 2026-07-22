"""
Use Case 2 orchestrator - the proposal's process flow, end to end:

  email mailbox / folder ingestion
    -> document classification & metadata extraction (one LLM call per
       document, with Portfolio Name-to-Code mapping applied)
    -> workflow routing to Investment / Operations teams
    -> cross-repository correlation by Trade ID / Portfolio Code
    -> filing into Year/Month/Date/Transaction structure
    -> audit-pack evaluation + compilation into the centralized audit
       repository
    -> metadata repository (search) + Excel report

Documents are analysed concurrently (same worker ceiling as Use Case 1;
each mostly waits on one LLM HTTP call) but correlation, filing, and pack
compilation are sequential and deterministic.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from app.utils.logger import get_logger
from config.settings import settings
from ops.analyzer import analyze_item
from ops.audit_pack import compile_pack, evaluate_pack
from ops.correlator import correlate
from ops.filer import file_document
from ops.intake import IntakeItem, gather_from_folder, gather_from_mailbox, gather_from_sharepoint
from ops.models import AuditPack, OpsDocument
from ops.ops_config import load_workflow
from ops.report import build_ops_report
from ops.repository import save_batch

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str], None]]


def _emit(progress_cb: ProgressCallback, message: str) -> None:
    logger.info(message)
    if progress_cb:
        try:
            progress_cb(message)
        except Exception:  # noqa: BLE001
            # progress_cb is purely informational (e.g. `print` in headless
            # CLI mode, which can't encode a Unicode character on a legacy
            # Windows console codepage) - a failure here must never bubble
            # up into a caller's try/except and get mistaken for the
            # document itself having failed. logger.info() above already
            # has a real record of the message even if this display step
            # couldn't render it.
            logger.exception("progress_cb failed for message: %r", message)


def _route(doc: OpsDocument) -> None:
    """'Automatically route incoming client instructions to the designated
    Investment or Operations Team per the configured workflow.'"""
    routing = load_workflow().get("routing", {})
    tx_type = doc.meta("transaction_type")
    doc.assigned_team = routing.get(tx_type, routing.get("default", "Operations Team"))


def run_ops_batch(
    intake_dir: Path | None = None,
    items: list[IntakeItem] | None = None,
    use_mailbox: bool = False,
    use_sharepoint: bool = False,
    progress_cb: ProgressCallback = None,
) -> tuple[list[OpsDocument], list[AuditPack], Path]:
    """
    Processes one intake batch through the full Use Case 2 flow. Returns
    (documents, audit packs, Excel report path). Sources combine: an
    explicit item list, a folder, the configured IMAP mailbox, and/or the
    configured SharePoint input folder.
    """
    gathered: list[IntakeItem] = list(items or [])
    if intake_dir is not None:
        gathered.extend(gather_from_folder(intake_dir))
    if use_mailbox:
        _emit(progress_cb, "Checking the configured mailbox for new instructions...")
        gathered.extend(gather_from_mailbox())
    if use_sharepoint:
        _emit(progress_cb, "Checking the configured SharePoint input folder for new instructions...")
        gathered.extend(gather_from_sharepoint())

    if not gathered:
        _emit(progress_cb, "No documents to process.")
        report_path = build_ops_report([], [])
        return [], [], report_path

    _emit(progress_cb, f"Analysing {len(gathered)} document(s) - classification + metadata extraction...")
    workers = max(1, settings.max_workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        documents = list(pool.map(analyze_item, gathered))

    for doc in documents:
        _route(doc)
        _emit(
            progress_cb,
            f"{doc.source_file}: {doc.doc_type_name} "
            f"({doc.classification_confidence:.0f}%) -> {doc.assigned_team}"
            + (f"  [review: {'; '.join(doc.review_flags)}]" if doc.review_flags else ""),
        )

    _emit(progress_cb, "Correlating documents into transactions...")
    groups = correlate(documents)
    _emit(progress_cb, f"{len(groups)} transaction(s) identified.")

    _emit(progress_cb, "Filing documents (Year/Month/Date/Transaction)...")
    for doc in documents:
        if not doc.error:
            file_document(doc)

    _emit(progress_cb, "Evaluating and compiling audit packs...")
    packs: list[AuditPack] = []
    for group in groups:
        pack = evaluate_pack(group)
        compile_pack(pack)
        packs.append(pack)
        _emit(progress_cb, f"  {group.transaction_key}: {pack.status}")

    _emit(progress_cb, "Saving to the metadata repository...")
    save_batch(documents, packs)

    report_path = build_ops_report(documents, packs)
    _emit(progress_cb, f"Ops report saved to {report_path}")
    return documents, packs, report_path
