"""
IMAP intake connector — pulls PDF instruction forms sent by clients to the
contact centre inbox (Section 3, Step 1: "A client submits a UT instruction
form (PDF) via email to the contact center").

Works with any standard IMAP mailbox — Office 365 / Outlook, a hosted BIFM
mailbox, Gmail-via-IMAP, etc. Uses only Python's built-in `imaplib` and
`email` modules, so there's no OAuth flow, no client-secret JSON, and no
extra dependency to install.

Required environment variables (see config.settings.ImapSettings):
    IMAP_HOST              - mail server hostname, e.g. mail.bifm.co.bw
    IMAP_PORT              - default 993 (IMAP over SSL)
    IMAP_USERNAME          - mailbox address, e.g. ut-instructions@bifm.co.bw
    IMAP_PASSWORD          - mailbox password (or app password, if your
                              provider requires one for IMAP — Gmail does
                              when 2-Step Verification is enabled)
    IMAP_USE_SSL           - default true
    IMAP_FOLDER            - mailbox/folder to search (default "INBOX")
    IMAP_SEARCH_CRITERIA   - IMAP SEARCH string (default "UNSEEN")
    IMAP_PROCESSED_FOLDER  - folder to move processed messages into, if the
                              server supports it (default "BIFM-Processed")

If IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD aren't all set, is_configured()
returns False and the app falls back to the other intake channels.
"""
from __future__ import annotations

import email
import imaplib
from email.header import decode_header
from email.message import Message
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)


def is_configured() -> bool:
    im = settings.imap
    return bool(im.host and im.username and im.password)


def _connect() -> imaplib.IMAP4:
    im = settings.imap
    conn: imaplib.IMAP4 = (
        imaplib.IMAP4_SSL(im.host, im.port) if im.use_ssl else imaplib.IMAP4(im.host, im.port)
    )
    conn.login(im.username, im.password)
    return conn


def check_connection() -> bool:
    """Lightweight connectivity/auth check for the sidebar status panel."""
    if not is_configured():
        return False
    try:
        conn = _connect()
        try:
            conn.select(settings.imap.folder, readonly=True)
        finally:
            conn.logout()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("IMAP connectivity check failed")
        return False


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = ""
    for chunk, encoding in parts:
        if isinstance(chunk, bytes):
            decoded += chunk.decode(encoding or "utf-8", errors="replace")
        else:
            decoded += chunk
    return decoded


def _ensure_processed_folder(conn: imaplib.IMAP4, folder_name: str) -> bool:
    """Best-effort create-if-missing for the processed folder. Returns
    False (without raising) if the server rejects it — plenty of hosted
    IMAP setups restrict folder creation, in which case we just leave
    messages marked \\Seen in place instead of moving them."""
    if not folder_name:
        return False
    try:
        typ, _ = conn.select(folder_name, readonly=True)
        if typ == "OK":
            return True
        conn.create(folder_name)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Could not create/select IMAP folder %r — leaving messages in place", folder_name)
        return False


def _iter_pdf_attachments(msg: Message):
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header_value(filename)
        if not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        yield filename, payload


def fetch_pdf_attachments(dest_dir: Path, search_criteria: str | None = None) -> list[dict[str, Any]]:
    """
    Searches the configured IMAP mailbox/folder for messages matching
    `search_criteria` (default: settings.imap.search_criteria, i.e.
    "UNSEEN"), downloads every PDF attachment to dest_dir, and marks the
    message \\Seen (moving it to the processed folder too, if configured
    and supported) so it isn't picked up again.

    Returns a list of {"path": Path, "sender": str, "subject": str,
    "message_id": str} dicts — one per downloaded PDF.
    """
    if not is_configured():
        return []

    im = settings.imap
    criteria = search_criteria or im.search_criteria
    dest_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    conn = _connect()
    try:
        typ, _ = conn.select(im.folder)
        if typ != "OK":
            logger.error("Could not select IMAP folder %r", im.folder)
            return []

        typ, data = conn.search(None, criteria)
        if typ != "OK":
            logger.error("IMAP search failed with criteria %r", criteria)
            return []

        message_nums = data[0].split()
        can_move = _ensure_processed_folder(conn, im.processed_folder) if im.processed_folder else False
        # Re-select the intake folder — _ensure_processed_folder's `select`
        # calls above may have switched the connection's current mailbox.
        conn.select(im.folder)

        for num in message_nums:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            sender = _decode_header_value(msg.get("From", "unknown"))
            subject = _decode_header_value(msg.get("Subject", "(no subject)"))
            message_id = msg.get("Message-ID", num.decode())

            downloaded_any = False
            for filename, pdf_bytes in _iter_pdf_attachments(msg):
                dest_path = dest_dir / filename
                counter = 1
                while dest_path.exists():
                    dest_path = dest_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                    counter += 1
                dest_path.write_bytes(pdf_bytes)
                downloaded_any = True
                results.append({
                    "path": dest_path,
                    "sender": sender,
                    "subject": subject,
                    "message_id": message_id,
                })
                logger.info("Downloaded IMAP attachment %s from %s -> %s", filename, sender, dest_path)

            if downloaded_any:
                conn.store(num, "+FLAGS", "\\Seen")
                if can_move:
                    try:
                        conn.copy(num, im.processed_folder)
                        conn.store(num, "+FLAGS", "\\Deleted")
                    except Exception:  # noqa: BLE001
                        logger.warning("Could not move message %s to %r", num, im.processed_folder)

        if can_move:
            conn.expunge()
    finally:
        conn.logout()

    return results
