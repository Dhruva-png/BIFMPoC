"""
Use Case 2 document intake.

"Monitor the designated email mailbox for incoming client instructions
related to Withdrawals and Contributions; automatically retrieve emails
and attachments."

Three sources, all normalised into IntakeItem records:
  - IMAP mailbox (reuses Use Case 1's app.connectors.imap_client - same
    mailbox settings, no new configuration surface). Attachments are
    downloaded as PDFs.
  - .eml files dropped in a folder (exported emails). The email BODY is
    itself an audit document for Ops - the proposal's withdrawal pack
    explicitly includes the "Investment Team Approval / Instruction
    Email" - so the body is materialised as a .txt sidecar and PDF
    attachments are unpacked next to it.
  - Plain files (PDF / TXT) dropped in a folder.

Every item carries where it came from (the metadata table's "source -
Email, Banking Platform, Internal System") and, for emails, the date the
instruction was received (the metadata table's Email Date).
"""
from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.logger import get_logger
from ops.ops_config import OPS_INTAKE_DIR, ensure_output_dirs

logger = get_logger(__name__)


@dataclass
class IntakeItem:
    path: Path                    # file to analyse (pdf or txt)
    kind: str                     # "pdf" | "text"
    source_label: str             # Email / Folder drop / Banking Platform ...
    email_date: str = ""          # ISO date the instruction was received
    email_subject: str = ""
    email_sender: str = ""
    body_text: str = ""           # populated for kind == "text"
    origin_file: str = ""         # the .eml this came out of, if any
    extra: dict = field(default_factory=dict)


def _parse_eml(eml_path: Path, dest_dir: Path) -> list[IntakeItem]:
    """One .eml -> the body as a text item + every PDF attachment."""
    items: list[IntakeItem] = []
    msg = email.message_from_bytes(eml_path.read_bytes(), policy=email.policy.default)

    subject = str(msg.get("Subject", "") or "")
    sender = str(msg.get("From", "") or "")
    email_date = ""
    if msg.get("Date"):
        try:
            email_date = email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
        except Exception:  # noqa: BLE001
            email_date = ""

    body = msg.get_body(preferencelist=("plain", "html"))
    body_text = body.get_content() if body else ""
    if body_text.strip():
        body_path = dest_dir / f"{eml_path.stem}_body.txt"
        body_path.write_text(body_text, encoding="utf-8")
        items.append(IntakeItem(
            path=body_path, kind="text", source_label="Email",
            email_date=email_date, email_subject=subject, email_sender=sender,
            body_text=body_text, origin_file=eml_path.name,
        ))

    for part in msg.iter_attachments():
        filename = part.get_filename() or ""
        if not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        dest = dest_dir / filename
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
            counter += 1
        dest.write_bytes(payload)
        items.append(IntakeItem(
            path=dest, kind="pdf", source_label="Email",
            email_date=email_date, email_subject=subject, email_sender=sender,
            origin_file=eml_path.name,
        ))

    if not items:
        logger.warning("%s: no usable body text or PDF attachments", eml_path.name)
    return items


def gather_from_folder(folder: Path) -> list[IntakeItem]:
    """Collects every PDF / EML / TXT in `folder` (non-recursive) into
    intake items. EMLs are unpacked (body + attachments); TXT files are
    treated as email-body-like text documents."""
    ensure_output_dirs()
    items: list[IntakeItem] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            items.append(IntakeItem(path=path, kind="pdf", source_label="Folder drop"))
        elif suffix == ".eml":
            items.extend(_parse_eml(path, OPS_INTAKE_DIR))
        elif suffix == ".txt":
            items.append(IntakeItem(
                path=path, kind="text", source_label="Folder drop",
                body_text=path.read_text(encoding="utf-8", errors="replace"),
            ))
    logger.info("Ops intake: %d item(s) from %s", len(items), folder)
    return items


def gather_from_mailbox() -> list[IntakeItem]:
    """Pulls PDF attachments from the configured IMAP mailbox via Use Case
    1's connector (same IMAP_* settings). Returns [] when the mailbox
    isn't configured."""
    from app.connectors import imap_client  # deferred: not needed for folder runs

    if not imap_client.is_configured():
        logger.info("IMAP not configured - mailbox intake skipped")
        return []
    ensure_output_dirs()
    downloads = imap_client.fetch_pdf_attachments(OPS_INTAKE_DIR)
    return [
        IntakeItem(
            path=d["path"], kind="pdf", source_label="Email",
            email_subject=d.get("subject", ""), email_sender=d.get("sender", ""),
        )
        for d in downloads
    ]
