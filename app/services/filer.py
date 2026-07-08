"""
Module 5: Document Filer (Section 5.3 / 7.2).

Renames and copies each source PDF using the naming convention:
    [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]

Also simulates the SharePoint folder structure confirmed in Section 6 of
the understanding document ("Current State and Marvel.ai Output
Placement"), entirely on the local filesystem, since live SharePoint
write-back is out of scope for this POC. Each of the six instruction
types follows its OWN filing convention — they are not interchangeable:

    New Business:     Submissions -> Received -> Approved/<Y>/<M>/<D> | Rejected
    Additional Inv.:  <Y>/<M> (dump, awaiting e-stamp) -> <D>/MM|NMM/Captured[/Approved|Rejected]
    Disinvestment:    <Y>/<M>/<D>/MM|NMM/Captured|Approved|Rejected
    Disinvestment-GSGF: <Y>/Q<n>-<Y>/<quarter-end date>/Captured|Approved|Rejected
    Debit Order:      <Y>/<M> (dump) -> Send to AWD/<D> | Rejected
    Static:           <Y>/<M> (dump) -> Send to AWD/<D> | Rejected
    KYC:              flat holding area (companion doc, Section 9.1)

Rejected files get a reason appended to the filename, matching the
client's own example: "...wrong banking details". See
resolve_destination_dir() for the exact per-form-type routing logic, kept
as a pure, swappable function so plugging in a real SharePoint client
later only means changing the destination-write step, not the routing
logic itself.
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

# Top-level folder (sibling of "New Business", "Additional Investments", etc.
# in the SharePoint-mirror structure) used to flag a batch that's missing an
# important companion document (e.g. a New Business submission with no KYC
# supporting document attached). This doesn't replace the normal per-document
# filing above — every document is still filed to its own routed destination
# — it's an extra marker folder so ops can see, at a glance in the folder
# tree, which batches are incomplete and need chasing.
MISSING_DOCS_DIRNAME = "Missing"


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
    """Money Market (cut-off 1PM) vs Non-Money Market (3PM) segregation."""
    cat = (fund_category or "").lower()
    if "money market" in cat and "non" not in cat:
        return "Money Market"
    return "Non-Money Market"


def _quarter_label(as_of: datetime) -> str:
    q = (as_of.month - 1) // 3 + 1
    return f"Q{q}-{as_of.year}"


def _gsgf_quarter_end_folder(as_of: datetime) -> str:
    """Last month of the current calendar quarter, per the doc's GSGF example
    ('30-Sep-2026 (last month of quarter -- submission by 7th Sep)')."""
    quarter_end_month = ((as_of.month - 1) // 3 + 1) * 3
    # Approximate last day of that month
    if quarter_end_month == 12:
        last_day = datetime(as_of.year, 12, 31)
    else:
        last_day = datetime(as_of.year, quarter_end_month + 1, 1)
        last_day = last_day.replace(day=1)
        from calendar import monthrange
        last_day = datetime(as_of.year, quarter_end_month, monthrange(as_of.year, quarter_end_month)[1])
    return last_day.strftime("%d-%b-%Y")


def resolve_destination_dir(
    form_code: str,
    fund_category: str | None,
    instruction_status: str,
    as_of: datetime | None = None,
) -> Path:
    """
    Builds the SharePoint-simulation destination folder using the exact
    per-form-type structure confirmed in Section 6 of the understanding
    document (BIFM UT Retail's existing filing convention) — each
    instruction type follows a different path, not one generic structure:

      NB (New Business / APPFORM):  New Business / Submissions|Received|
                                     Approved/<Year>/<Month>/<Day>|Rejected
      ADD (Additional Investment):  Additional Investments / <Year>/<Month>
                                     [ / <Day> / MoneyMarket|NonMoneyMarket
                                       / Captured[/Approved|Rejected] ]
      DIS (Disinvestment Standard): Disinvestments / <Year>/<Month>/<Day>
                                     / MoneyMarket|NonMoneyMarket
                                     / Captured|Approved|Rejected
      DIS_GSG (GSGF Disinvestment): Disinvestments/GSGF / <Year>
                                     / Q<n>-<Year> / <last-month-end date>
                                     / Captured|Approved|Rejected
      DEBIT (Debit Order):          Debit Orders / <Year>/<Month>
                                     [ / Send to AWD / <Day> | Rejected ]
      STATIC (Change of Details):   Static / <Year>/<Month>
                                     [ / Send to AWD / <Day> | Rejected ]
      KYC (supporting document):    filed alongside its parent instruction's
                                     stage folder — this POC doesn't model
                                     the parent link at file-routing time, so
                                     KYC docs are held in a flat "KYC"
                                     holding folder pending that linkage
                                     (Section 9.1: KYC is a companion
                                     document, not a standalone instruction).
    """
    as_of = as_of or datetime.now()
    base = settings.paths.filed_dir
    year = as_of.strftime("%Y")
    month = as_of.strftime("%m-%B")
    day = as_of.strftime("%d-%b-%Y")
    rejected = instruction_status == InstructionStatus.REJECTED.value
    approved = instruction_status == InstructionStatus.APPROVED.value

    if form_code == "APPFORM":
        if rejected:
            return base / "New Business" / "Rejected"
        if approved:
            return base / "New Business" / "Approved" / year / month.split("-", 1)[1] / day
        # Submitted/Captured both live in the flat pre-AWD queues; Captured
        # (dual-capture done, awaiting authoriser) maps to "Received".
        if instruction_status == InstructionStatus.CAPTURED.value:
            return base / "New Business" / "Received"
        return base / "New Business" / "Submissions"

    if form_code == "ADD":
        month_dir = base / "Additional Investments" / year / month.split("-", 1)[1]
        if instruction_status == InstructionStatus.SUBMITTED.value:
            return month_dir  # dumped here on receipt, awaiting e-stamp
        # After e-stamp / fund-receipt confirmation: dated day -> MM/NMM -> Captured[/Approved|Rejected]
        status_leaf = "Captured"
        if rejected:
            status_leaf = "Captured/Rejected"
        elif approved:
            status_leaf = "Captured/Approved"
        return month_dir / day / _fund_bucket(fund_category).replace(" ", "").replace("-", "") / status_leaf

    if form_code == "DIS":
        status_leaf = "Captured"
        if rejected:
            status_leaf = "Rejected"
        elif approved:
            status_leaf = "Approved"
        return base / "Disinvestments" / year / month.split("-", 1)[1] / day / _fund_bucket(fund_category) / status_leaf

    if form_code == "DIS_GSG":
        status_leaf = "Captured"
        if rejected:
            status_leaf = "Rejected"
        elif approved:
            status_leaf = "Approved"
        return (
            base / "Disinvestments" / "GSGF" / year / _quarter_label(as_of)
            / _gsgf_quarter_end_folder(as_of) / status_leaf
        )

    if form_code in ("DEBIT", "STATIC"):
        top = "Debit Orders" if form_code == "DEBIT" else "Static"
        month_dir = base / top / year / month.split("-", 1)[1]
        if instruction_status == InstructionStatus.SUBMITTED.value:
            return month_dir
        if rejected:
            # A rejected instruction was never queued for AWD authorization -
            # filing it under "Send to AWD" alongside genuinely-captured
            # ones (as every rejected DEBIT/STATIC form previously was) is
            # misleading and inconsistent with how APPFORM/ADD/DIS/DIS_GSG
            # all separate their Rejected instructions out.
            return month_dir / "Rejected"
        return month_dir / "Send to AWD" / day

    # KYC and any unrecognised form code: flat holding area pending linkage
    # to its parent instruction (out of scope for this POC per Section 9.1).
    return base / "KYC" / year / month.split("-", 1)[1]


def flag_missing_documents(
    person_key: str,
    missing_labels: list[str],
    source_files: list[str] | None = None,
    as_of: datetime | None = None,
) -> Path | None:
    """
    Creates/updates a marker file under the top-level "Missing" folder for a
    batch that's missing one or more important documents (e.g. a New
    Business form with no KYC companion document, or a third-party payment
    with no Form B banking annex) — the pre-validation flags engine
    (app.validation.prevalidation) is what determines which documents count
    as "missing" for a given batch; this function just files the result.

    No-op (returns None) if missing_labels is empty — nothing to flag.
    """
    if not missing_labels:
        return None

    as_of = as_of or datetime.now()
    base = settings.paths.filed_dir
    destination_dir = base / MISSING_DOCS_DIRNAME
    destination_dir.mkdir(parents=True, exist_ok=True)

    date_str = as_of.strftime("%Y%m%d")
    marker_name = f"{_clean_token(person_key)}_{date_str}.txt"
    destination = destination_dir / marker_name

    lines = [
        f"Batch: {person_key}",
        f"Flagged: {as_of.isoformat(timespec='seconds')}",
        f"Source document(s): {', '.join(source_files) if source_files else 'N/A'}",
        "Missing / incomplete:",
        *[f"  - {label}" for label in missing_labels],
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "Flagged batch '%s' as missing %d document(s) -> %s",
        person_key, len(missing_labels), destination,
    )
    return destination


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
    destination_dir = resolve_destination_dir(form_code, fund_category, instruction_status, as_of)
    destination = destination_dir / new_filename

    # Avoid silent overwrite if two forms hash to the same name on the same day.
    counter = 1
    while destination.exists():
        destination = destination_dir / f"{destination.stem}_{counter}{destination.suffix}"
        counter += 1

    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, destination)
    logger.info("Filed %s -> %s", source_pdf.name, destination.relative_to(settings.paths.filed_dir))

    if settings.sharepoint.write_back_enabled:
        from app.connectors import sharepoint_client
        remote_folder = str(destination.relative_to(settings.paths.filed_dir).parent).replace("\\", "/")
        sharepoint_client.upload_file(destination, remote_folder)

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