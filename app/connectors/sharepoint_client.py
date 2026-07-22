from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def is_configured() -> bool:
    sp = settings.sharepoint
    return bool(sp.tenant_id and sp.client_id and sp.client_secret and sp.site_id)


def _get_access_token() -> str:
    """Client-credentials token, cached until ~60s before expiry."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    sp = settings.sharepoint
    url = f"https://login.microsoftonline.com/{sp.tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": sp.client_id,
        "client_secret": sp.client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_get_access_token()}"}


def _drive_root() -> str:
    """Graph URL segment for the configured site's drive."""
    sp = settings.sharepoint
    if sp.drive_id:
        return f"{GRAPH_BASE}/drives/{sp.drive_id}"
    return f"{GRAPH_BASE}/sites/{sp.site_id}/drive"


def check_connection() -> bool:
    """Lightweight connectivity check for the sidebar status panel."""
    if not is_configured():
        return False
    try:
        resp = requests.get(f"{_drive_root()}/root", headers=_headers(), timeout=15)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        logger.exception("SharePoint connectivity check failed")
        return False


def list_new_files(folder_path: str, extensions: tuple[str, ...] = (".pdf",)) -> list[dict[str, Any]]:
    """
    Lists files sitting directly in the given SharePoint folder whose name
    ends in one of `extensions` (case-insensitive). Returns a list of
    {"id", "name", "download_url", "size"} dicts. General-purpose version
    of list_new_pdfs - Use Case 2's intake also needs .eml/.txt, not just
    .pdf, so this takes the extension set as a parameter instead of
    hard-coding ".pdf".
    """
    url = f"{_drive_root()}/root:/{folder_path}:/children"
    items: list[dict[str, Any]] = []
    exts = tuple(e.lower() for e in extensions)
    while url:
        resp = requests.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        for entry in payload.get("value", []):
            name = entry.get("name", "")
            if "file" in entry and name.lower().endswith(exts):
                items.append({
                    "id": entry["id"],
                    "name": name,
                    "download_url": entry.get("@microsoft.graph.downloadUrl"),
                    "size": entry.get("size", 0),
                })
        url = payload.get("@odata.nextLink")
    return items


def list_new_pdfs(folder_path: str | None = None) -> list[dict[str, Any]]:
    """Lists PDF files sitting directly in the configured UT submission
    folder. Thin wrapper over list_new_files for UT's existing call sites -
    behaviour is unchanged from before list_new_files existed."""
    return list_new_files(folder_path or settings.sharepoint.submission_folder, (".pdf",))


def download_file(item: dict[str, Any], dest_dir: Path) -> Path:
    """Downloads one SharePoint item (from list_new_pdfs) to dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / item["name"]
    download_url = item.get("download_url")
    if download_url:
        resp = requests.get(download_url, timeout=60)
    else:
        resp = requests.get(f"{_drive_root()}/items/{item['id']}/content", headers=_headers(), timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    logger.info("Downloaded SharePoint file %s -> %s", item["name"], dest_path)
    return dest_path


def fetch_folder(
    dest_dir: Path, folder_path: str, extensions: tuple[str, ...] = (".pdf",),
    move_processed_to: str | None = None,
) -> list[Path]:
    """
    Downloads every file matching `extensions` from `folder_path` into
    dest_dir. General-purpose version of fetch_submission_folder.

    move_processed_to: if given, each successfully-downloaded item is
    moved (not deleted - nothing is destroyed, just relocated within the
    same site) from `folder_path` to this folder afterward, so a repeat
    run doesn't see it again and re-process it into a duplicate
    transaction. None (the default, and what fetch_submission_folder
    below uses) leaves items in place, matching this function's original
    behaviour before this parameter existed.
    """
    if not is_configured():
        return []
    items = list_new_files(folder_path, extensions)
    downloaded = [download_file(item, dest_dir) for item in items]
    if move_processed_to:
        for item in items:
            delete_or_move_source(item["id"], move_processed_to)
    return downloaded


def fetch_submission_folder(dest_dir: Path) -> list[Path]:
    """UT's PDF-only submission folder fetch. Thin wrapper over
    fetch_folder - behaviour is unchanged from before fetch_folder existed."""
    return fetch_folder(dest_dir, settings.sharepoint.submission_folder, (".pdf",))


def upload_file(local_path: Path, folder_path: str) -> bool:
    if not is_configured():
        return False
    try:
        remote_path = f"{folder_path.strip('/')}/{local_path.name}"
        url = f"{_drive_root()}/root:/{remote_path}:/content"
        data = local_path.read_bytes()
        headers = {**_headers(), "Content-Type": "application/octet-stream"}
        resp = requests.put(url, headers=headers, data=data, timeout=60)
        resp.raise_for_status()
        logger.info("Uploaded %s -> SharePoint:/%s", local_path.name, remote_path)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("SharePoint upload failed for %s", local_path.name)
        return False


def delete_or_move_source(item_id: str, target_folder_path: str) -> bool:
    if not is_configured():
        return False
    try:
        url = f"{_drive_root()}/items/{item_id}"
        body = {"parentReference": {"path": f"/drive/root:/{target_folder_path}"}}
        resp = requests.patch(url, headers={**_headers(), "Content-Type": "application/json"}, json=body, timeout=30)
        resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("SharePoint move failed for item %s", item_id)
        return False
