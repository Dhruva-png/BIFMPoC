"""
Filing for Use Case 2: "store them in the configured folder structure per
BIFM's business rules (Year -> Month -> Date -> Transaction)".

Each analysed document is copied (never moved - the intake original stays
put as the source of record) into:

    ops_output/filed/<Year>/<Month name>/<DD>/<TransactionKey>/<filename>

The date used is the transaction date where one was extracted, falling
back to the email-received date, then today - so undated documents still
land in a deterministic place instead of erroring.

When settings.sharepoint.ops_write_back_enabled is set, the same file is
also pushed to SharePoint at the identical relative path under
settings.sharepoint.ops_filed_folder - mirroring Use Case 1's
app.services.filer.file_document exactly (local structure computed once,
then mirrored, not recomputed, on SharePoint).
"""
from __future__ import annotations

import re
import shutil
from datetime import date, datetime
from pathlib import Path

from app.utils.logger import get_logger
from config.settings import settings
from ops.models import OpsDocument
from ops.ops_config import OPS_FILED_DIR, ensure_output_dirs

logger = get_logger(__name__)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._\- ]")


def _safe(name: str) -> str:
    return _SAFE_RE.sub("_", str(name)).strip() or "UNSPECIFIED"


def _filing_date(doc: OpsDocument) -> date:
    for field_id in ("transaction_date", "email_date"):
        try:
            return datetime.fromisoformat(doc.meta(field_id)).date()
        except (TypeError, ValueError):
            continue
    return date.today()


def file_document(doc: OpsDocument) -> Path:
    """Copies the document into the Year/Month/Date/Transaction structure
    and records the destination on the document. Returns the destination."""
    ensure_output_dirs()
    when = _filing_date(doc)
    dest_dir = (
        OPS_FILED_DIR
        / f"{when.year}"
        / when.strftime("%B")
        / f"{when.day:02d}"
        / _safe(doc.transaction_key or "UNCORRELATED")
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / Path(doc.source_path).name
    counter = 1
    while dest.exists() and dest.resolve() != Path(doc.source_path).resolve():
        dest = dest_dir / f"{dest.stem}_{counter}{dest.suffix}"
        counter += 1
    if not dest.exists():
        shutil.copy2(doc.source_path, dest)
    doc.filed_path = str(dest)
    logger.info("Filed %s -> %s", doc.source_file, dest)

    if settings.sharepoint.ops_write_back_enabled:
        from app.connectors import sharepoint_client
        remote_folder = f"{settings.sharepoint.ops_filed_folder}/{dest.relative_to(OPS_FILED_DIR).parent}".replace("\\", "/")
        sharepoint_client.upload_file(dest, remote_folder)

    return dest
