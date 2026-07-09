"""
Module 5: Document Filer (Section 5.3 / 7.2).

Renames and copies each source PDF using one of two naming conventions,
depending on outcome:

  PASS / WARNING (no rejection):
      [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]
      e.g. "ADD_ENT12345_MODISE_20260630.pdf"
      Matches the README's documented Document Naming Convention exactly -
      uppercased, underscore-separated, so it's both human- and
      machine-parseable in the filed-documents folder listing.

  Rejected:
      [FormType] - [InvestorFullName] - [RejectionReason].[ext]
      e.g. "ADD - Alomelo - missing bank details.pdf"
      No entity number / date on rejected files — short form so Sales can
      read the rejection reason at a glance in the folder listing. Case is
      preserved here (unlike the PASS/WARNING format) to match how a
      rejection reason is naturally phrased.

Missing/incomplete companion-document batches (Section 8/9.1 - e.g. a New
Business submission with no KYC form attached) get a similar reason-
bearing marker filename in the top-level "Missing" folder - see
flag_missing_documents() below.

InstructionDateSigned is the date the client actually signed the form -
the "Instruction Date (signed)" metadata field from Section 6 of the
understanding document - not the date the system happened to process it;
falls back to the processing date only when a signed date wasn't
captured. This follows Section 6's own metadata model (Instruction Date
and Upload Date are two distinct fields there) instead of collapsing
everything to a single system timestamp.

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

See resolve_destination_dir() for the exact per-form-type folder routing
logic (independent of filename convention above), kept as a pure,
swappable function so plugging in a real SharePoint client later only
means changing the destination-write step, not the routing logic itself.
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
    """
    Filename-safe token: uppercased, invalid characters stripped. Spaces
    become hyphens (not deleted) so a multi-word value reads as
    "WRONG-BANKING-DETAILS" instead of being squashed into one illegible
    run of letters.

    Used only by flag_missing_documents' marker filename — build_filename
    uses _clean_token_preserve_case instead (see below) so PDF/Excel
    filenames keep their natural casing.
    """
    value = (value or "UNKNOWN").strip().upper().replace(" ", "-")
    return _INVALID_FILENAME_CHARS.sub("", value) or "UNKNOWN"


# Filesystem-forbidden characters only (Windows' reserved set covers
# Linux/macOS too) — used by _clean_token_preserve_case so filenames keep
# their original casing and spacing instead of being uppercased/hyphenated.
_INVALID_FILENAME_CHARS_SAFE = re.compile(r'[\\/:*?"<>|]')


def _clean_token_preserve_case(value: str) -> str:
    """
    Filename-safe token that preserves original casing/spacing - strips
    only characters the filesystem itself won't allow, and collapses/
    trims whitespace. Used by build_filename so "Amolemo" stays
    "Amolemo" and "wrong banking details" stays lowercase with spaces,
    matching the client's own naming examples.
    """
    value = (value or "UNKNOWN").strip()
    value = _INVALID_FILENAME_CHARS_SAFE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "UNKNOWN"


# Same parseable formats validation/engine.py accepts for date fields -
# duplicated here (rather than imported) to keep filer.py's only
# dependency on extracted data being the plain strings passed into it.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y")


def _parse_signed_date(value) -> datetime | None:
    """Parses the extracted 'date_signed' field into a datetime, trying
    every format the extractor/validator can produce. Returns None (never
    raises) if the value is blank or doesn't match any known format, so
    the caller can fall back to the processing date."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def build_filename(
    form_code: str,
    entity_number: str,
    investor_name: str,
    ext: str = "pdf",
    as_of: datetime | None = None,
    rejection_reason: str | None = None,
    date_signed: str | None = None,
) -> str:
    """
    Pure function. Two formats depending on outcome:

    PASS / WARNING (rejection_reason is empty/None):
        [FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].[ext]
        e.g. "ADD_ENT12345_MODISE_20260630.pdf"

        Uppercased, underscore-separated, matching the README's documented
        Document Naming Convention. investor_name is uppercased and
        spaces become hyphens (via _clean_token) so a multi-word name
        stays readable rather than being squashed together.

        - date_signed: the extracted "Instruction Date (signed)" field,
          per Section 6 - used in place of the processing date whenever
          it parses successfully. Falls back to as_of/now if blank or
          unparseable.

    Rejected (rejection_reason is provided):
        [FormType] - [InvestorFullName] - [RejectionReason].[ext]
        e.g. "ADD - Alomelo - missing bank details.pdf"

        Deliberately omits entity number and date - kept short so the
        reason is visible at a glance in the folder listing. Casing/
        spacing is preserved here (unlike the PASS/WARNING format above),
        matching how a rejection reason is naturally phrased (e.g. "missing
        bank details", not "MISSING-BANK-DETAILS").
    """
    as_of = as_of or datetime.now()

    if rejection_reason:
        form_token = _clean_token_preserve_case(form_code)
        name_token = _clean_token_preserve_case(investor_name)
        reason_token = _clean_token_preserve_case(rejection_reason)
        return f"{form_token} - {name_token} - {reason_token}.{ext.lstrip('.')}"

    effective_date = _parse_signed_date(date_signed) or as_of
    date_str = effective_date.strftime("%Y%m%d")
    form_token = _clean_token(form_code)
    entity_token = _clean_token(entity_number)
    name_token = _clean_token(investor_name)
    return f"{form_token}_{entity_token}_{name_token}_{date_str}.{ext.lstrip('.')}"


def _fund_bucket(fund_category: str | None) -> str:
    """
    Money Market (cut-off 1PM) vs Non-Money Market (3PM) segregation.

    Returns a single, filesystem-safe, space/hyphen-free token
    ("MoneyMarket" / "NonMoneyMarket") so every form type routes into the
    SAME folder for the same bucket. Previously this returned "Money
    Market" / "Non-Money Market" and only the ADD branch normalized it
    afterwards (`.replace(" ", "").replace("-", "")`) — the DIS branch used
    the raw value directly, so the same investor's Additional Investment
    and Disinvestment documents landed in differently-named sibling
    folders (e.g. "MoneyMarket" vs "Money Market") instead of the one
    correct folder. Normalizing here, once, means every caller is
    consistent by construction and there's nothing left to forget.
    """
    cat = (fund_category or "").lower()
    if "money market" in cat and "non" not in cat:
        return "MoneyMarket"
    return "NonMoneyMarket"


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
        return month_dir / day / _fund_bucket(fund_category) / status_leaf

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
    reasons: list[str] | None = None,
    form_code: str | None = None,
) -> Path | None:
    """
    Creates/updates a marker file under the top-level "Missing" folder for a
    batch that's missing one or more important documents (e.g. a New
    Business form with no KYC companion document, or a third-party payment
    with no Form B banking annex) — the pre-validation flags engine
    (app.validation.prevalidation) is what determines which documents count
    as "missing" for a given batch; this function just files the result.

    The marker FILENAME itself (not just its body text) carries the form
    type, person, and reason(s) so ops can see what's missing straight from
    the folder listing without opening the file - e.g.
    "APPFORM - Amolemo - KYC document absent.txt" - matching the same
    "reason visible in the filename" principle as a rejected instruction's
    filename (see build_filename above). `reasons` should be the short flag
    label(s) (e.g. PreValidationFlag.label), not the longer per-document
    explanation - that longer text still goes in the file body below.
    Falls back to `missing_labels` itself if `reasons` isn't given, and to
    "Missing" for form_code when the batch's triggering form type isn't
    known to the caller.

    No-op (returns None) if missing_labels is empty — nothing to flag.
    """
    if not missing_labels:
        return None

    as_of = as_of or datetime.now()
    base = settings.paths.filed_dir
    destination_dir = base / MISSING_DOCS_DIRNAME
    destination_dir.mkdir(parents=True, exist_ok=True)

    date_str = as_of.strftime("%Y%m%d")
    form_token = _clean_token_preserve_case(form_code) if form_code else "Missing"
    name_token = _clean_token_preserve_case(person_key)
    reason_token = _clean_token_preserve_case(", ".join(reasons or missing_labels))
    marker_name = f"{form_token} - {name_token} - {reason_token} - {date_str}.txt"
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
    date_signed: str | None = None,
) -> ProcessingLogEntry:
    """
    Copies source_pdf into the routed SharePoint-simulation folder under its
    standardized name and returns a ProcessingLogEntry ready to append to the
    Processing Log sheet.

    fund_category is still used for folder ROUTING (Money Market vs
    Non-Money Market queues, see resolve_destination_dir/_fund_bucket) even
    though it's no longer part of the filename itself.

    date_signed: the extracted "Instruction Date (signed)" field (Section
    6) - used in the filename in place of today's processing date when
    it's present and parses cleanly, and only for the PASS/WARNING naming
    format (see build_filename).
    """
    as_of = datetime.now()
    new_filename = build_filename(
        form_code, entity_number, full_name or "UNKNOWN",
        ext=source_pdf.suffix.lstrip("."), as_of=as_of,
        rejection_reason=rejection_reason or None,
        date_signed=date_signed,
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