from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from app.connectors import imap_client, sharepoint_client
from app.utils.logger import get_logger
from config.settings import settings

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
            # underlying operation having failed. logger.info() above
            # already has a real record of the message even if this
            # display step couldn't render it.
            logger.exception("progress_cb failed for message: %r", message)


def sharepoint_available() -> bool:
    return sharepoint_client.is_configured()


def imap_available() -> bool:
    return imap_client.is_configured()


def pull_from_sharepoint(progress_cb: ProgressCallback = None) -> list[Path]:
    """
    Downloads every PDF currently sitting in the configured SharePoint
    submission folder into a local staging directory, ready to be handed
    to app.core.pipeline.process_batch. Channel is tagged "Walk-in/Email"
    upstream on SharePoint (Section 3, Step 2) so this connector doesn't
    re-derive it — the batch-level channel selector in the UI covers it.
    """
    if not sharepoint_client.is_configured():
        _emit(progress_cb, "SharePoint is not configured — set SHAREPOINT_TENANT_ID / "
                            "CLIENT_ID / CLIENT_SECRET / SITE_ID to enable this source.")
        return []

    dest_dir = settings.paths.intake_dir / "_sharepoint"
    _emit(progress_cb, f"Checking SharePoint folder '{settings.sharepoint.submission_folder}' for new forms...")
    try:
        paths = sharepoint_client.fetch_submission_folder(dest_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("SharePoint fetch failed")
        _emit(progress_cb, f"ERROR fetching from SharePoint: {exc}")
        return []

    _emit(progress_cb, f"Pulled {len(paths)} document(s) from SharePoint.")
    return paths


def pull_from_imap(progress_cb: ProgressCallback = None) -> list[Path]:
    """
    Downloads PDF attachments from unread/matching emails in the
    configured IMAP inbox into a local staging directory, and marks each
    processed message \\Seen (moving it to a processed folder too, if
    configured) so it isn't re-pulled next run.
    """
    if not imap_client.is_configured():
        _emit(progress_cb, "Email intake is not configured — set IMAP_HOST / IMAP_USERNAME / "
                            "IMAP_PASSWORD to enable this source.")
        return []

    dest_dir = settings.paths.intake_dir / "_imap"
    _emit(progress_cb, f"Searching '{settings.imap.folder}' ('{settings.imap.search_criteria}') for instruction forms...")
    try:
        attachments = imap_client.fetch_pdf_attachments(dest_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("IMAP fetch failed")
        _emit(progress_cb, f"ERROR fetching from email inbox: {exc}")
        return []

    for att in attachments:
        _emit(progress_cb, f"  {att['path'].name} (from {att['sender']}, subject: \"{att['subject']}\")")
    _emit(progress_cb, f"Pulled {len(attachments)} document(s) from email inbox.")
    return [att["path"] for att in attachments]
