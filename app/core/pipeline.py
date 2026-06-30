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
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.models.schemas import ExtractionResult, ProcessingLogEntry, ValidationReport
from app.ocr.pdf_utils import render_pdf_to_images
from app.services import classifier, filer
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


def process_single_document(
    pdf_path: Path,
    report: ExcelReportBuilder,
    progress_cb: ProgressCallback = None,
) -> DocumentOutcome:
    """
    Runs one document through the full 5-stage pipeline.
    Never raises — errors are captured in the returned DocumentOutcome.
    """
    try:
        # Stage 1: Render PDF pages to images
        _emit(progress_cb, f"Rendering pages for {pdf_path.name}...")
        page_images = render_pdf_to_images(pdf_path)

        # Stage 2: Classify the form type
        _emit(progress_cb, f"Classifying {pdf_path.name}...")
        classification = classifier.classify_form(page_images[0])

        # Disambiguate GSG vs Standard disinvestment when confidence is borderline
        if classification.form_code in ("DIS", "DIS_GSG") and classification.confidence < HIGH_CONFIDENCE_MIN:
            _emit(progress_cb, f"Disambiguating DIS vs DIS_GSG for {pdf_path.name}...")
            classification = classifier.disambiguate_gsg_vs_standard(page_images[0])

        form_code = classification.form_code
        _emit(progress_cb, f"{pdf_path.name}: classified as '{classification.form_name}' ({classification.confidence:.0f}% confidence)")

        # Stage 3: Extract fields (ALL form types now go through extraction)
        _emit(progress_cb, f"Extracting fields from {pdf_path.name} ({form_code})...")
        extraction = extract_form(form_code, page_images)

        # Stage 4: Validate the extraction
        _emit(progress_cb, f"Validating {pdf_path.name}...")
        validation_report = validate(extraction)

        # Stage 5: File the document with standardized naming
        _emit(progress_cb, f"Filing {pdf_path.name}...")
        # Resolve entity_number: use extracted value or fall back to id_number / source file name
        entity_number = (
            extraction.field_value("entity_number")
            or extraction.field_value("id_number")
            or "N/A"
        )
        full_name = extraction.field_value("full_name")

        log_entry = filer.file_document(
            pdf_path,
            form_code=form_code,
            entity_number=str(entity_number),
            full_name=full_name,
            validation_status=validation_report.overall_status.value,
            classification_confidence=classification.confidence,
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


def run_batch(
    intake_dir: Path | None = None,
    progress_cb: ProgressCallback = None,
) -> tuple[list[DocumentOutcome], Path]:
    """
    Processes every PDF in intake_dir (default: settings.paths.intake_dir).
    Returns per-document outcomes in stable file order and the path to the
    saved Excel report.

    Documents are processed concurrently (settings.max_workers, default 2).
    Each document spends most of its time waiting on the Ollama HTTP call,
    so overlapping that wait with another document's page rendering (CPU-bound)
    cuts wall-clock time even without extra GPU capacity.
    """
    intake_dir = intake_dir or settings.paths.intake_dir
    pdf_files = sorted(intake_dir.glob("*.pdf"))

    if not pdf_files:
        _emit(progress_cb, f"No PDF files found in {intake_dir}")
        return [], settings.paths.output_dir / settings.excel_report_name

    _emit(progress_cb, f"Found {len(pdf_files)} document(s) to process (up to {settings.max_workers} in parallel).")
    report = ExcelReportBuilder()

    results: dict[int, DocumentOutcome] = {}
    with ThreadPoolExecutor(max_workers=max(1, settings.max_workers)) as pool:
        futures = {
            pool.submit(process_single_document, pdf, report, progress_cb): i
            for i, pdf in enumerate(pdf_files)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    outcomes = [results[i] for i in range(len(pdf_files))]
    output_path = report.save()
    _emit(progress_cb, f"Batch complete. Report saved to {output_path}")
    return outcomes, output_path
