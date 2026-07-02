"""
Orchestrates a full document processing run across the 5 modules:

  intake PDF → render pages → Module 1 (Classify) → Module 2 (Extract)
             → Module 3 (Validate) → Module 4 (Excel rows) → Module 5 (File)

ALL 6 BIFM UT form types are now fully processed through extraction and
validation — not just classified and filed with a "NOT_EXTRACTED" placeholder.
Every form type goes through:
  1. Classification (which form type is this?)
  2. Full field extraction (form-type-specific field set)
  3. Validation (mandatory fields, format checks, derived fields)
  4. Excel report row
  5. Document filing with standardized filename

BATCH = ONE PERSON (see process_batch / app.services.consolidator):
Every batch handed to this app - one intake folder, one Streamlit upload -
is one investor's set of forms from a single visit (e.g. STATIC + DIS +
DEBIT + ADD dropped in together). Investors routinely fill in identity/
contact/banking fields on only one of those forms and leave them blank on
the rest, assuming the office already has the details from the form right
next to it. process_batch runs classification+extraction for every
document first, merges the person-level fields into one profile, backfills
each document's blanks from that profile, and only *then* validates and
files - so a Debit Order that only had 4 fields filled in by hand can still
pass with entity_number/full_name/contact_number/email pulled in from the
sibling Static form in the same batch.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from app.models.schemas import (
    ClassificationResult,
    ExtractionResult,
    InstructionStatus,
    ProcessingLogEntry,
    ValidationReport,
)
from app.ocr.pdf_utils import render_pdf_to_images
from app.services import classifier, filer
from app.services.consolidator import backfill_from_profile, build_person_profile
from app.services.extractor import extract_form
from app.services.report_generator import ExcelReportBuilder
from app.utils.config_loader import load_validation_rules
from app.utils.logger import get_logger
from app.validation.engine import validate
from config.settings import settings

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str], None]]

HIGH_CONFIDENCE_MIN = load_validation_rules()["confidence_thresholds"]["high"]["min"]


@dataclass
class DocumentOutcome:
    filename: str
    form_code: str
    classification_confidence: float
    validation_status: str
    error: str | None = None
    extraction: ExtractionResult | None = None
    validation: ValidationReport | None = None


def _emit(progress_cb: ProgressCallback, message: str) -> None:
    logger.info(message)
    if progress_cb:
        progress_cb(message)


def _person_key_for_batch(pdf_paths: list[Path]) -> str:
    """
    BIFM's own intake naming convention is "<FormType> - <Surname>.pdf"
    (e.g. "STATIC - AMOLEMO.pdf", "DIS - AMOLEMO.pdf"). Since a batch is one
    person, the surname token after " - " is a convenient human-readable
    label for the consolidated profile row. Falls back to the first file's
    stem if that convention isn't followed.
    """
    first = pdf_paths[0].stem
    if " - " in first:
        return first.split(" - ")[-1].strip() or first
    return first


def _extract_only(
    pdf_path: Path,
    progress_cb: ProgressCallback = None,
) -> tuple[Optional[ClassificationResult], Optional[ExtractionResult], Optional[str]]:
    """
    Stages 1-3: render → classify → extract. Returns (classification,
    extraction, error). Never raises — a failure comes back as
    (None, None, "message") so a bad document can't take the rest of the
    batch's consolidation/validation down with it.
    """
    try:
        _emit(progress_cb, f"Rendering pages for {pdf_path.name}...")
        page_images = render_pdf_to_images(pdf_path)

        _emit(progress_cb, f"Classifying {pdf_path.name}...")
        classification = classifier.classify_form(page_images[0])

        if classification.form_code in ("DIS", "DIS_GSG") and classification.confidence < HIGH_CONFIDENCE_MIN:
            _emit(progress_cb, f"Disambiguating DIS vs DIS_GSG for {pdf_path.name}...")
            classification = classifier.disambiguate_gsg_vs_standard(page_images[0])

        form_code = classification.form_code
        _emit(progress_cb, f"{pdf_path.name}: classified as '{classification.form_name}' ({classification.confidence:.0f}% confidence)")

        _emit(progress_cb, f"Extracting fields from {pdf_path.name} ({form_code})...")
        extraction = extract_form(form_code, page_images)

        return classification, extraction, None

    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process %s", pdf_path.name)
        _emit(progress_cb, f"ERROR processing {pdf_path.name}: {exc}")
        return None, None, str(exc)


def _validate_and_file(
    pdf_path: Path,
    classification: ClassificationResult,
    extraction: ExtractionResult,
    report: ExcelReportBuilder,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
) -> DocumentOutcome:
    """
    Stages 4-5: validate the (possibly backfilled) extraction, file the
    document, and add its row to the Excel report. Never raises — a
    failure comes back as an "ERROR" DocumentOutcome.
    """
    try:
        upload_date = datetime.fromtimestamp(pdf_path.stat().st_mtime).isoformat(timespec="seconds")
        form_code = classification.form_code

        _emit(progress_cb, f"Validating {pdf_path.name}...")
        validation_report = validate(extraction)

        _emit(progress_cb, f"Filing {pdf_path.name}...")
        # Resolve entity_number: use extracted value or fall back to id_number / source file name
        entity_number = (
            extraction.field_value("entity_number")
            or extraction.field_value("id_number")
            or "N/A"
        )
        full_name = extraction.field_value("full_name")
        fund_category = extraction.field_value("fund_category")

        if validation_report.overall_status.value == "FAIL":
            instruction_status = InstructionStatus.REJECTED.value
            failed = [r.field_id for r in validation_report.results if r.status.value == "FAIL"]
            rejection_reason = f"Validation failed: {', '.join(failed)}" if failed else "Validation failed"
        else:
            instruction_status = InstructionStatus.CAPTURED.value
            rejection_reason = ""

        log_entry = filer.file_document(
            pdf_path,
            form_code=form_code,
            entity_number=str(entity_number),
            full_name=full_name,
            validation_status=validation_report.overall_status.value,
            classification_confidence=classification.confidence,
            fund_category=str(fund_category) if fund_category else None,
            instruction_status=instruction_status,
            rejection_reason=rejection_reason,
            channel=channel,
            upload_date=upload_date,
        )

        report.add_form(extraction, validation_report, log_entry)
        _emit(progress_cb, f"{pdf_path.name}: complete → {validation_report.overall_status.value}")

        return DocumentOutcome(
            filename=pdf_path.name,
            form_code=form_code,
            classification_confidence=classification.confidence,
            validation_status=validation_report.overall_status.value,
            extraction=extraction,
            validation=validation_report,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process %s", pdf_path.name)
        _emit(progress_cb, f"ERROR processing {pdf_path.name}: {exc}")
        return DocumentOutcome(
            filename=pdf_path.name,
            form_code="ERROR",
            classification_confidence=0.0,
            validation_status="FAIL",
            error=str(exc),
        )


def process_single_document(
    pdf_path: Path,
    report: ExcelReportBuilder,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
) -> DocumentOutcome:
    """
    Runs one document through the full 5-stage pipeline in isolation, with
    no cross-document backfilling. Kept for callers that genuinely have a
    single standalone document. For a normal batch (multiple forms from
    the same investor), use process_batch instead so blank identity/
    contact/banking fields get filled in from sibling documents before
    validation.

    Never raises — errors are captured in the returned DocumentOutcome.
    channel: "Email" or "Walk-in" — tagged at upload per the requirements
    doc's "Channel (Email / Walk-in) - Tagged at upload" metadata field.
    """
    classification, extraction, error = _extract_only(pdf_path, progress_cb)
    if error or classification is None or extraction is None:
        return DocumentOutcome(
            filename=pdf_path.name,
            form_code="ERROR",
            classification_confidence=0.0,
            validation_status="FAIL",
            error=error or "Extraction failed",
        )
    return _validate_and_file(pdf_path, classification, extraction, report, progress_cb, channel)


def process_batch(
    pdf_paths: list[Path],
    report: ExcelReportBuilder,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
    max_workers: int | None = None,
) -> list[DocumentOutcome]:
    """
    Runs a batch of documents belonging to ONE PERSON through the pipeline:

      1. Stage 1-3 (render/classify/extract) for every document, concurrently.
      2. Build one consolidated person profile from every successful
         extraction (app.services.consolidator.build_person_profile) and
         backfill each document's blank identity/contact/banking fields
         from it - this is what lets a Debit Order form that only had 4
         fields filled in by hand pick up entity_number/full_name/
         contact_number/email from the Static form sitting next to it in
         the same batch, instead of failing validation on fields that
         genuinely were provided, just on a different page.
      3. Stage 4-5 (validate/file/report) for every document, using the
         backfilled extraction.
      4. Add one consolidated profile row to the "Consolidated Investor
         Profile" report sheet for the whole batch.

    Returns per-document outcomes in stable input order. Never raises —
    per-document failures are captured in their DocumentOutcome and don't
    stop the rest of the batch.
    """
    if not pdf_paths:
        return []

    workers = max(1, max_workers if max_workers is not None else settings.max_workers)

    extracted: dict[int, tuple[Path, Optional[ClassificationResult], Optional[ExtractionResult], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_extract_only, pdf, progress_cb): i
            for i, pdf in enumerate(pdf_paths)
        }
        for future in as_completed(futures):
            i = futures[future]
            classification, extraction, error = future.result()
            extracted[i] = (pdf_paths[i], classification, extraction, error)

    ordered = [extracted[i] for i in range(len(pdf_paths))]

    # Build the person-level profile from every document that extracted
    # cleanly, then backfill each document's blanks from it.
    good_extractions = [e for (_, _, e, err) in ordered if e is not None and err is None]
    profile = build_person_profile(good_extractions)
    if profile:
        _emit(
            progress_cb,
            f"Consolidating {len(good_extractions)} document(s) in this batch — "
            f"{len(profile)} person-level field(s) available to backfill.",
        )
        for _, _, extraction, err in ordered:
            if extraction is None or err is not None:
                continue
            backfilled_fields = backfill_from_profile(extraction, profile)
            if backfilled_fields:
                _emit(
                    progress_cb,
                    f"{extraction.source_file}: backfilled {', '.join(backfilled_fields)} "
                    "from other documents in this batch",
                )

    outcomes: list[DocumentOutcome] = []
    for pdf_path, classification, extraction, error in ordered:
        if error or classification is None or extraction is None:
            outcomes.append(DocumentOutcome(
                filename=pdf_path.name,
                form_code="ERROR",
                classification_confidence=0.0,
                validation_status="FAIL",
                error=error or "Extraction failed",
            ))
            continue
        outcomes.append(_validate_and_file(pdf_path, classification, extraction, report, progress_cb, channel))

    if profile:
        person_key = _person_key_for_batch(pdf_paths)
        source_files = [p.name for p in pdf_paths]
        report.add_consolidated_profile(profile, person_key=person_key, source_files=source_files)
        _emit(progress_cb, f"Consolidated profile for '{person_key}' added ({len(source_files)} document(s)).")

    return outcomes


def run_batch(
    intake_dir: Path | None = None,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
) -> tuple[list[DocumentOutcome], Path]:
    """
    Processes every PDF in intake_dir (default: settings.paths.intake_dir)
    as one batch/one person via process_batch. Returns per-document
    outcomes in stable file order and the path to the saved Excel report.

    channel: "Email" or "Walk-in" — applied to every document in this batch
    (the UI offers this as a per-batch choice, since submission channel is
    typically consistent within one intake drop).

    Documents are extracted concurrently (settings.max_workers, default 2)
    before consolidation. Each document spends most of its time waiting on
    the LLM HTTP call, so overlapping that wait with another document's
    page rendering (CPU-bound) cuts wall-clock time even without extra
    GPU/API capacity.
    """
    intake_dir = intake_dir or settings.paths.intake_dir
    pdf_files = sorted(intake_dir.glob("*.pdf"))

    if not pdf_files:
        _emit(progress_cb, f"No PDF files found in {intake_dir}")
        return [], settings.paths.output_dir / settings.excel_report_name

    _emit(progress_cb, f"Found {len(pdf_files)} document(s) to process (up to {settings.max_workers} in parallel).")
    report = ExcelReportBuilder()

    outcomes = process_batch(pdf_files, report, progress_cb, channel)

    output_path = report.save()
    _emit(progress_cb, f"Batch complete. Report saved to {output_path}")
    return outcomes, output_path
