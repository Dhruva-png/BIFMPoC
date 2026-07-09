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
    IMAP_MIN_UNREAD_MINUTES - minimum time (minutes) a message must have
                              sat unread before it's processed (default 60)
    IMAP_ALLOWED_SENDERS    - comma-separated list of sender email
                              addresses allowed to submit instruction
                              forms; a message from anyone else is left
                              unread and skipped. Blank disables the check
                              (every sender allowed). Matched against the
                              bare address only (case-insensitive) - a
                              display name in the From header, e.g. "Jane
                              Doe <jane@example.com>", doesn't need to
                              match exactly.

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
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

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


def _allowed_sender_set() -> set[str]:
    """Parses IMAP_ALLOWED_SENDERS into a lowercase address set. Empty set
    means the check is disabled (every sender allowed)."""
    raw = settings.imap.allowed_senders
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


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

    Only messages that have been sitting unread for at least
    settings.imap.min_unread_minutes (default 60) are processed - this is
    a MINIMUM age, not a recency window, so a message that's been unread
    for days is just as eligible as one that crossed the threshold a
    minute ago. Keeps a just-arrived, possibly still-incomplete submission
    (e.g. a client sending the form and a follow-up attachment moments
    apart) from being grabbed and processed prematurely.

    Returns a list of {"path": Path, "sender": str, "subject": str,
    "message_id": str} dicts — one per downloaded PDF.
    """
    if not is_configured():
        return []

    im = settings.imap
    criteria = search_criteria or im.search_criteria
    dest_dir.mkdir(parents=True, exist_ok=True)

    min_age = timedelta(minutes=im.min_unread_minutes)
    age_cutoff = datetime.now(timezone.utc) - min_age

    # IMAP SEARCH date keys (SINCE/BEFORE) only have day-level granularity,
    # so they can't express "at least N minutes old" directly - the actual
    # cutoff is enforced per-message below, from each message's real Date
    # header. This SINCE bound is just a coarse efficiency guard against
    # scanning a mailbox's entire history; it's set well before the age
    # cutoff so it never excludes a message that's still genuinely eligible.
    search_floor = (datetime.now() - min_age - timedelta(days=1)).strftime("%d-%b-%Y")
    search_query = f'({criteria} SINCE "{search_floor}")'

    results: list[dict[str, Any]] = []
    conn = _connect()

    try:
        typ, _ = conn.select(im.folder)
        if typ != "OK":
            logger.error("Could not select IMAP folder %r", im.folder)
            return []

        typ, data = conn.search(None, search_query)
        if typ != "OK":
            logger.error("IMAP search failed with criteria %r", search_query)
            return []

        message_nums = data[0].split()

        can_move = (
            _ensure_processed_folder(conn, im.processed_folder)
            if im.processed_folder
            else False
        )

        # Re-select the intake folder in case it changed
        conn.select(im.folder)

        for num in message_nums:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Only process a message once it's been unread for at least
            # min_unread_minutes - if we can't determine its age at all
            # (missing/unparseable Date header), skip it rather than risk
            # processing a just-arrived, possibly-incomplete submission.
            date_header = msg.get("Date")
            email_time = None
            if date_header:
                try:
                    email_time = parsedate_to_datetime(date_header)
                    if email_time.tzinfo is None:
                        email_time = email_time.replace(tzinfo=timezone.utc)
                except Exception:  # noqa: BLE001
                    email_time = None

            if email_time is None:
                logger.warning(
                    "Could not determine age of message %s (missing/unparseable "
                    "Date header) — skipping until it can be re-checked",
                    num.decode(),
                )
                continue

            if email_time > age_cutoff:
                logger.info(
                    "Skipping message %s — unread for less than %d minute(s)",
                    num.decode(), im.min_unread_minutes,
                )
                continue

            sender = _decode_header_value(msg.get("From", "unknown"))
            subject = _decode_header_value(msg.get("Subject", "(no subject)"))
            message_id = msg.get("Message-ID", num.decode())

            allowed_senders = _allowed_sender_set()
            if allowed_senders:
                _, sender_email = parseaddr(msg.get("From", ""))
                if sender_email.lower() not in allowed_senders:
                    logger.info(
                        "Skipping IMAP message %s from %s (sender not in "
                        "IMAP_ALLOWED_SENDERS)",
                        message_id, sender,
                    )
                    continue

            downloaded_any = False

            for filename, pdf_bytes in _iter_pdf_attachments(msg):
                dest_path = dest_dir / filename
                counter = 1

                while dest_path.exists():
                    dest_path = (
                        dest_dir
                        / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                    )
                    counter += 1

                dest_path.write_bytes(pdf_bytes)

                downloaded_any = True
                results.append(
                    {
                        "path": dest_path,
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                    }
                )

                logger.info(
                    "Downloaded IMAP attachment %s from %s -> %s",
                    filename,
                    sender,
                    dest_path,
                )

            if downloaded_any:
                conn.store(num, "+FLAGS", "\\Seen")

                if can_move:
                    try:
                        conn.copy(num, im.processed_folder)
                        conn.store(num, "+FLAGS", "\\Deleted")
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Could not move message %s to %r",
                            num.decode(),
                            im.processed_folder,
                        )

        if can_move:
            conn.expunge()

    finally:
        conn.logout()

    return results