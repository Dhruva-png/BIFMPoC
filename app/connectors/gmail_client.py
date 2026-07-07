"""
Gmail intake connector — pulls PDF instruction forms sent by clients to the
contact centre inbox (Section 3, Step 1: "A client submits a UT instruction
form (PDF) via email to the contact center").

Auth: standard Gmail API OAuth installed-app flow (google-auth-oauthlib).
On first use, a browser consent window opens; the resulting refresh token
is cached to GMAIL_TOKEN_FILE so subsequent runs are non-interactive.

Required environment variables (see config.settings.GmailSettings):
    GMAIL_CREDENTIALS_FILE - path to the OAuth client secret JSON downloaded
                              from Google Cloud Console (Desktop app type)
    GMAIL_TOKEN_FILE        - where the cached user token is stored (default:
                              config/gmail_token.json)
    GMAIL_QUERY             - Gmail search query for candidate instruction
                              emails (default: "has:attachment filename:pdf
                              is:unread")
    GMAIL_LABEL_PROCESSED   - label applied after a message's attachments
                              have been downloaded, so it isn't re-pulled

If GMAIL_CREDENTIALS_FILE isn't set/doesn't exist, is_configured() returns
False and the app falls back to the other intake channels.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_service_cache: Any = None


def is_configured() -> bool:
    gm = settings.gmail
    return bool(gm.credentials_file and Path(gm.credentials_file).exists())


def _get_service():
    """Builds (and caches) an authenticated Gmail API client."""
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    gm = settings.gmail
    token_path = Path(gm.token_file)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(gm.credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    _service_cache = build("gmail", "v1", credentials=creds)
    return _service_cache


def check_connection() -> bool:
    """Lightweight connectivity/auth check for the sidebar status panel."""
    if not is_configured():
        return False
    try:
        service = _get_service()
        service.users().getProfile(userId="me").execute()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Gmail connectivity check failed")
        return False


def fetch_pdf_attachments(dest_dir: Path, query: str | None = None) -> list[dict[str, Any]]:
    """
    Searches Gmail for messages matching `query` (default: unread messages
    with a PDF attachment), downloads every PDF attachment to dest_dir, and
    labels the message as processed so it isn't picked up again.

    Returns a list of {"path": Path, "sender": str, "subject": str,
    "message_id": str} dicts — one per downloaded PDF.
    """
    if not is_configured():
        return []

    service = _get_service()
    query = query or settings.gmail.query
    dest_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    label_id = _ensure_label(service, settings.gmail.label_processed)

    page_token = None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page_token).execute()
        for msg_meta in resp.get("messages", []):
            msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "unknown")
            subject = headers.get("Subject", "(no subject)")

            downloaded_any = False
            for part in _iter_parts(msg.get("payload", {})):
                filename = part.get("filename", "")
                if not filename.lower().endswith(".pdf"):
                    continue
                body = part.get("body", {})
                attachment_id = body.get("attachmentId")
                if attachment_id:
                    att = service.users().messages().attachments().get(
                        userId="me", messageId=msg_meta["id"], id=attachment_id,
                    ).execute()
                    data = att.get("data", "")
                else:
                    data = body.get("data", "")
                if not data:
                    continue
                pdf_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
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
                    "message_id": msg_meta["id"],
                })
                logger.info("Downloaded Gmail attachment %s from %s -> %s", filename, sender, dest_path)

            if downloaded_any and label_id:
                service.users().messages().modify(
                    userId="me", id=msg_meta["id"],
                    body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
                ).execute()

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def _iter_parts(payload: dict[str, Any]):
    """Flattens Gmail's nested MIME part tree."""
    if "parts" in payload:
        for part in payload["parts"]:
            yield from _iter_parts(part)
    else:
        yield payload


def _ensure_label(service, label_name: str) -> str | None:
    if not label_name:
        return None
    try:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label["name"] == label_name:
                return label["id"]
        created = service.users().labels().create(
            userId="me", body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        return created["id"]
    except Exception:  # noqa: BLE001
        logger.exception("Could not ensure Gmail label %r", label_name)
        return None
