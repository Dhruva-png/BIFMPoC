"""
Module 5: Document Filer (Section 5.3 / 7.2).

Renames and copies each source PDF using the naming convention:
    [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]

Also simulates the SharePoint folder structure described in the current
process flow (Step 2-5 of the requirements doc), entirely on the local
filesystem, since live SharePoint write-back is out of scope for this POC
(Graph API integration is the natural next step per Section 7.3):

    output/filed_documents/
        <Year>/<Month>/<Day>/                  <- "submission folder" (Step 2)
            MoneyMarket/       or NonMoneyMarket/   <- Step 3 segregation
                Captured/                            <- Step 4
                    Approved/   or   Rejected/       <- Step 5
                                                        (Rejected files get a
                                                        reason appended to the
                                                        filename, matching the
                                                        client's own example:
                                                        "...wrong banking details")

This keeps the naming/foldering logic as a pure, swappable function so
plugging in a real SharePoint client later only means changing the
destination-write step, not the routing logic itself.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from app.models.schemas import ProcessingLogEntry, InstructionStatus
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

_INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_\-]")


def _clean_token(value: str) -> str:
    value = (value or "UNKNOWN").strip().upper().replace(" ", "")
    return _INVALID_FILENAME_CHARS.sub("", value) or "UNKNOWN"


def build_filename(
    form_code: str,
    entity_number: str,
    surname: str,
    ext: str = "pdf",
    as_of: datetime | None = None,
    rejection_reason: str | None = None,
) -> str:
    """
    Pure function: [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]
    (Section 5.3). If rejection_reason is supplied, it's appended per the
    client's own example: "...appended with wrong banking details".
    """
    as_of = as_of or datetime.now()
    date_str = as_of.strftime("%Y%m%d")
    base = f"{_clean_token(form_code)}_{_clean_token(entity_number)}_{_clean_token(surname)}_{date_str}"
    if rejection_reason:
        base = f"{base}_{_clean_token(rejection_reason)}"
    return f"{base}.{ext.lstrip('.')}"


def _extract_surname(full_name: str | None) -> str:
    if not full_name:
        return "UNKNOWN"
    parts = full_name.strip().split()
    return parts[-1] if parts else "UNKNOWN"


def _fund_bucket(fund_category: str | None) -> str:
    """Step 3 segregation: Money Market (cut-off 1PM/12PM) vs Non-Money Market (3PM/quarterly)."""
    cat = (fund_category or "").lower()
    if "money market" in cat and "non" not in cat:
        return "MoneyMarket"
    return "NonMoneyMarket"


def _status_bucket(instruction_status: str) -> str:
    """
    Step 4/5 folder: everything lands in Captured first (this POC's
    equivalent of "processor logs the instruction"). Rejected items (a
    validation FAIL) move straight to Rejected, matching the client's
    described rejection flow. Approved is left for the human authorizer
    step in AWD (out of scope here) - PASS/WARNING items stay in Captured
    pending that sign-off rather than being auto-approved.
    """
    if instruction_status == InstructionStatus.REJECTED.value:
        return "Captured/Rejected"
    return "Captured"


def resolve_destination_dir(fund_category: str | None, instruction_status: str, as_of: datetime | None = None) -> Path:
    """Builds the Year/Month/Day -> MM|NMM -> Captured[/Rejected] folder path."""
    as_of = as_of or datetime.now()
    return (
        settings.paths.filed_dir
        / as_of.strftime("%Y")
        / as_of.strftime("%m-%B")
        / as_of.strftime("%d")
        / _fund_bucket(fund_category)
        / _status_bucket(instruction_status)
    )


def file_document(
    source_pdf: Path,
    form_code: str,
    entity_number: str,
    full_name: str | None,
    validation_status: str,
    classification_confidence: float,
    fund_category: str | None = None,
    instruction_status: str = InstructionStatus.CAPTURED.value,
    rejection_reason: str = "",
    channel: str = "Unknown",
    upload_date: str | None = None,
) -> ProcessingLogEntry:
    """
    Copies source_pdf into the routed SharePoint-simulation folder under its
    standardized name and returns a ProcessingLogEntry ready to append to the
    Processing Log sheet.
    """
    as_of = datetime.now()
    surname = _extract_surname(full_name)
    new_filename = build_filename(
        form_code, entity_number, surname, ext=source_pdf.suffix.lstrip("."),
        as_of=as_of, rejection_reason=rejection_reason or None,
    )
    destination_dir = resolve_destination_dir(fund_category, instruction_status, as_of)
    destination = destination_dir / new_filename

    # Avoid silent overwrite if two forms hash to the same name on the same day.
    counter = 1
    while destination.exists():
        destination = destination_dir / f"{destination.stem}_{counter}{destination.suffix}"
        counter += 1

    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, destination)
    logger.info("Filed %s -> %s", source_pdf.name, destination.relative_to(settings.paths.filed_dir))

    return ProcessingLogEntry.now(
        original_filename=source_pdf.name,
        new_filename=destination.name,
        form_type_detected=form_code,
        classification_confidence=classification_confidence,
        validation_status=validation_status,
        destination_path=str(destination),
        upload_date=upload_date or as_of.isoformat(timespec="seconds"),
        channel=channel,
        instruction_status=instruction_status,
        rejection_reason=rejection_reason,
    )
