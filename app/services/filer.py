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
MISSING_DOCS_DIRNAME = "Missing"


def _clean_token(value: str) -> str:
    value = (value or "UNKNOWN").strip().upper().replace(" ", "-")
    return _INVALID_FILENAME_CHARS.sub("", value) or "UNKNOWN"

_INVALID_FILENAME_CHARS_SAFE = re.compile(r'[\\/:*?"<>|]')


def _clean_token_preserve_case(value: str) -> str:
    value = (value or "UNKNOWN").strip()
    value = _INVALID_FILENAME_CHARS_SAFE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "UNKNOWN"

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y")


def _parse_signed_date(value) -> datetime | None:
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
            return month_dir / "Rejected" / day
        return month_dir / "Send to AWD" / day
    day_dir = base / "KYC" / year / month.split("-", 1)[1] / day
    if rejected:
        return day_dir / "Rejected"
    if approved:
        return day_dir / "Approved"
    return day_dir


def flag_missing_documents(
    person_key: str,
    missing_labels: list[str],
    source_files: list[str] | None = None,
    as_of: datetime | None = None,
    reasons: list[str] | None = None,
    form_code: str | None = None,
) -> Path | None:
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