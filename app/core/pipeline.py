"""
Orchestrates a full batch run across the 5 modules described in Section 7.2:

  intake PDF -> render pages -> Module 1 (Classify) -> Module 2 (Extract)
             -> Module 3 (Validate) -> Module 4 (Excel rows) -> Module 5 (File)

Designed to process one document fully before moving to the next, so a
failure on one form doesn't block the rest of the batch (Processing Log
captures per-document outcome either way).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.models.schemas import ExtractionResult, ProcessingLogEntry, ValidationReport
from app.ocr.pdf_utils import render_pdf_to_images
from app.services import classifier, filer
from app.services.extractor import extract_investment_application_form
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


def process_single_document(pdf_path: Path, report: ExcelReportBuilder, progress_cb: ProgressCallback = None) -> DocumentOutcome:
    """Runs one document through the full pipeline; never raises - errors are captured in the outcome."""
    try:
        _emit(progress_cb, f"Rendering pages for {pdf_path.name}...")
        page_images = render_pdf_to_images(pdf_path)

        _emit(progress_cb, f"Classifying {pdf_path.name}...")
        classification = classifier.classify_form(page_images[0])

        # Disambiguate GSG vs Standard disinvestment only when needed (see classifier.py docstring).
        if classification.form_code in ("DIS", "DIS_GSG") and classification.confidence < HIGH_CONFIDENCE_MIN:
            _emit(progress_cb, "Disambiguating GSG vs Standard disinvestment...")
            classification = classifier.disambiguate_gsg_vs_standard(page_images[0])

        if classification.form_code != "APPFORM":
            # Out of POC's primary extraction scope (Section 3.1) - classify & file only.
            log_entry = filer.file_document(
                pdf_path, classification.form_code, entity_number="N/A",
                full_name=None, validation_status="NOT_EXTRACTED",
                classification_confidence=classification.confidence,
            )
            empty_extraction = ExtractionResult(source_file=pdf_path.stem, form_code=classification.form_code)
            empty_validation = ValidationReport(source_file=pdf_path.stem, entity_number="N/A")
            report.add_form(empty_extraction, empty_validation, log_entry)
            _emit(progress_cb, f"{pdf_path.name}: classified as {classification.form_name} (extraction not in POC scope for this form type)")
            return DocumentOutcome(pdf_path.name, classification.form_code, classification.confidence, "NOT_EXTRACTED",
                                    extraction=empty_extraction, validation=empty_validation)

        _emit(progress_cb, f"Extracting fields from {pdf_path.name}...")
        extraction = extract_investment_application_form(page_images)

        _emit(progress_cb, f"Validating {pdf_path.name}...")
        validation_report = validate(extraction)

        _emit(progress_cb, f"Filing {pdf_path.name}...")
        log_entry = filer.file_document(
            pdf_path,
            form_code=classification.form_code,
            entity_number=validation_report.entity_number,
            full_name=extraction.field_value("full_name"),
            validation_status=validation_report.overall_status.value,
            classification_confidence=classification.confidence,
        )

        report.add_form(extraction, validation_report, log_entry)
        _emit(progress_cb, f"{pdf_path.name}: {validation_report.overall_status.value}")
        return DocumentOutcome(pdf_path.name, classification.form_code, classification.confidence, validation_report.overall_status.value,
                                extraction=extraction, validation=validation_report)

    except Exception as exc:  # noqa: BLE001 - one bad document must not kill the batch
        logger.exception("Failed to process %s", pdf_path.name)
        _emit(progress_cb, f"ERROR processing {pdf_path.name}: {exc}")
        return DocumentOutcome(pdf_path.name, "ERROR", 0.0, "FAIL", error=str(exc))


def run_batch(intake_dir: Path | None = None, progress_cb: ProgressCallback = None) -> tuple[list[DocumentOutcome], Path]:
    """
    Processes every PDF in intake_dir (default: settings.paths.intake_dir),
    returns the per-document outcomes (in stable, original file order) and the
    path to the saved Excel report.

    Documents are processed concurrently (settings.max_workers, default 2) -
    each document spends most of its time waiting on an HTTP call to Ollama,
    so overlapping that wait with another document's page rendering/image
    encoding (CPU-bound) cuts wall-clock time even without a faster model.
    ExcelReportBuilder.add_form() is only ever called from process_single_document,
    and list.append() is atomic under the GIL, so no extra locking is needed.
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
