"""
Module 5: Document Filer (Section 5.3 / 7.2).

Renames and copies each source PDF using the naming convention:
    [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]

Note (assumption, documented per Section 1.1 "What This POC Is NOT"):
live SharePoint write-back / automated folder creation is explicitly out of
scope for this POC. This module files documents into a local "filed_documents"
folder using the exact naming convention, so swapping in a real SharePoint
client later (Module 5 in Section 7.3 recommends Graph API) only requires
changing the destination-write step - the naming logic itself is reusable
and is intentionally kept as a pure function.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from app.models.schemas import ProcessingLogEntry
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

_INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_\-]")


def _clean_token(value: str) -> str:
    value = (value or "UNKNOWN").strip().upper().replace(" ", "")
    return _INVALID_FILENAME_CHARS.sub("", value) or "UNKNOWN"


def build_filename(form_code: str, entity_number: str, surname: str, ext: str = "pdf", as_of: datetime | None = None) -> str:
    """Pure function: [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext] (Section 5.3)."""
    as_of = as_of or datetime.now()
    date_str = as_of.strftime("%Y%m%d")
    return f"{_clean_token(form_code)}_{_clean_token(entity_number)}_{_clean_token(surname)}_{date_str}.{ext.lstrip('.')}"


def _extract_surname(full_name: str | None) -> str:
    if not full_name:
        return "UNKNOWN"
    parts = full_name.strip().split()
    return parts[-1] if parts else "UNKNOWN"


def file_document(
    source_pdf: Path,
    form_code: str,
    entity_number: str,
    full_name: str | None,
    validation_status: str,
    classification_confidence: float,
) -> ProcessingLogEntry:
    """
    Copies source_pdf into settings.paths.filed_dir under its standardized name
    and returns a ProcessingLogEntry ready to append to the Processing Log sheet.
    """
    surname = _extract_surname(full_name)
    new_filename = build_filename(form_code, entity_number, surname, ext=source_pdf.suffix.lstrip("."))
    destination = settings.paths.filed_dir / new_filename

    # Avoid silent overwrite if two forms hash to the same name on the same day.
    counter = 1
    while destination.exists():
        destination = settings.paths.filed_dir / f"{destination.stem}_{counter}{destination.suffix}"
        counter += 1

    settings.paths.filed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, destination)
    logger.info("Filed %s -> %s", source_pdf.name, destination.name)

    return ProcessingLogEntry.now(
        original_filename=source_pdf.name,
        new_filename=destination.name,
        form_type_detected=form_code,
        classification_confidence=classification_confidence,
        validation_status=validation_status,
        destination_path=str(destination),
    )
