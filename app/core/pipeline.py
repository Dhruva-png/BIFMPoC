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
    PreValidationFlag,
    ProcessingLogEntry,
    ValidationReport,
)
from app.ocr.pdf_utils import render_pdf_to_images
from app.services import classifier, filer
from app.services.filer import flag_missing_documents
from app.services.consolidator import backfill_from_profile, build_person_profile
from app.services.extractor import extract_form
from app.services.report_generator import ExcelReportBuilder, build_single_document_workbook
from app.services.query_register import QueryRegisterBuilder
from app.utils.config_loader import load_validation_rules
from app.utils.logger import get_logger
from app.validation.engine import validate
from app.validation.prevalidation import evaluate_prevalidation_flags
from config.settings import settings

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str], None]]

HIGH_CONFIDENCE_MIN = load_validation_rules()["confidence_thresholds"]["high"]["min"]

# Pre-validation flags (config/prevalidation_flags.json) that specifically mean
# "an important companion document appears to be missing from this batch" —
# as opposed to a field on the document itself being incomplete. When any of
# these trigger, the whole batch gets an extra marker in the "Missing" folder
# (app.services.filer.flag_missing_documents) so ops can spot incomplete
# batches straight from the folder tree, not just from the Excel report.
MISSING_DOCUMENT_FLAG_IDS = {
    "kyc_document_absent",
    "third_party_bank_form_b_absent",
    "acting_on_behalf_form_c_absent",
}


@dataclass
class DocumentOutcome:
    filename: str
    form_code: str
    classification_confidence: float
    validation_status: str
    error: str | None = None
    extraction: ExtractionResult | None = None
    validation: ValidationReport | None = None
    prevalidation_flags: list[PreValidationFlag] | None = None
    individual_report_path: str | None = None


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


def _person_key_for_file(pdf_path: Path) -> str:
    """
    Same "<FormType> - <Surname>.pdf" convention as _person_key_for_batch,
    applied to a SINGLE file rather than an already-known-single-person
    batch — used to group a mixed intake folder into one sub-batch per
    person before any consolidation happens. Falls back to the file's own
    stem when the convention isn't followed, which means a form that
    doesn't follow the naming convention is simply treated as its own
    (unmatched) person rather than silently merged into someone else's.
    """
    stem = pdf_path.stem
    if " - " in stem:
        return stem.split(" - ")[-1].strip() or stem
    return stem


def _group_by_person(pdf_paths: list[Path]) -> list[list[Path]]:
    """
    Splits a mixed intake folder into one group per person, using the
    surname token from BIFM's "<FormType> - <Surname>.pdf" naming
    convention, so a folder containing several investors' forms in one
    drop doesn't get treated as a single consolidated batch (which would
    incorrectly backfill/merge fields across different people and produce
    one shared "Consolidated Investor Profile" row / "Missing" flag for
    the whole folder instead of one per person). Order is preserved:
    groups appear in the order their first file was seen, and files within
    a group keep their original relative order.
    """
    groups: dict[str, list[Path]] = {}
    for pdf_path in pdf_paths:
        key = _person_key_for_file(pdf_path)
        groups.setdefault(key, []).append(pdf_path)
    return list(groups.values())


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

        # Disambiguate GSG vs Standard disinvestment when confidence is borderline
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
    batch_form_codes: list[str] | None = None,
    query_register: QueryRegisterBuilder | None = None,
) -> DocumentOutcome:
    """
    Stages 4-5: validate the (possibly backfilled) extraction, file the
    document, and add its row to the Excel report. Never raises — a
    failure comes back as an "ERROR" DocumentOutcome.

    batch_form_codes: form codes of every OTHER document in this person's
    batch, used by the pre-validation flags engine (Section 8) to detect
    companion documents (e.g. a KYC form dropped in alongside a New
    Business form).
    """
    try:
        upload_date = datetime.fromtimestamp(pdf_path.stat().st_mtime).isoformat(timespec="seconds")
        form_code = classification.form_code

        _emit(progress_cb, f"Validating {pdf_path.name}...")
        validation_report = validate(extraction)

        _emit(progress_cb, f"Checking pre-validation flags for {pdf_path.name}...")
        prevalidation_flags = evaluate_prevalidation_flags(
            extraction, form_code, batch_form_codes=batch_form_codes,
        )
        triggered_flags = [f for f in prevalidation_flags if f.triggered]
        if triggered_flags:
            _emit(
                progress_cb,
                f"{pdf_path.name}: {len(triggered_flags)} pre-validation flag(s) raised "
                f"({', '.join(f.label for f in triggered_flags)})",
            )

        _emit(progress_cb, f"Filing {pdf_path.name}...")
        # Resolve entity_number: use extracted value or fall back to id_number / source file name
        entity_number = (
            extraction.field_value("entity_number")
            or extraction.field_value("id_number")
            or "N/A"
        )
        full_name = extraction.field_value("full_name")
        fund_category = extraction.field_value("fund_category")

        # Instruction Status / Rejection Reason (Metadata Fields for Filing).
        # A validation FAIL means a mandatory field is missing/malformed —
        # matches the client's own rejection examples (e.g. "wrong banking
        # details"). WARNING/PASS stay "Captured", awaiting the human
        # authorizer's Approve/Reject decision in AWD (out of this POC's scope).
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

        report.add_form(extraction, validation_report, log_entry, prevalidation_flags=prevalidation_flags)
        if query_register is not None:
            query_register.add_form(extraction, validation_report, log_entry, prevalidation_flags=prevalidation_flags)

        # Individual, single-document workbook — saved right next to the
        # filed PDF (same folder, same base filename), so opening the
        # output folder shows each document with its own small report,
        # independent of the batch/consolidated workbook.
        single_doc_path: Path | None = None
        try:
            single_doc_wb = build_single_document_workbook(
                extraction, validation_report, log_entry, prevalidation_flags=prevalidation_flags,
            )
            single_doc_path = Path(log_entry.destination_path).with_suffix(".xlsx")
            single_doc_wb.save(single_doc_path)
            _emit(progress_cb, f"{pdf_path.name}: individual report saved -> {single_doc_path.name}")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to save individual report for %s", pdf_path.name)

        _emit(progress_cb, f"{pdf_path.name}: complete → {validation_report.overall_status.value}")

        return DocumentOutcome(
            filename=pdf_path.name,
            form_code=form_code,
            classification_confidence=classification.confidence,
            validation_status=validation_report.overall_status.value,
            extraction=extraction,
            validation=validation_report,
            prevalidation_flags=prevalidation_flags,
            individual_report_path=str(single_doc_path) if single_doc_path else None,
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
    query_register: QueryRegisterBuilder | None = None,
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
    return _validate_and_file(
        pdf_path, classification, extraction, report, progress_cb, channel,
        batch_form_codes=[], query_register=query_register,
    )


def process_batch(
    pdf_paths: list[Path],
    report: ExcelReportBuilder,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
    max_workers: int | None = None,
    query_register: QueryRegisterBuilder | None = None,
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

    # Every other successfully-classified document's form_code, for
    # companion-document detection in the pre-validation flags engine
    # (e.g. a KYC form present alongside a New Business form in this batch).
    all_form_codes = [c.form_code for (_, c, e, err) in ordered if c is not None and e is not None and err is None]

    outcomes: list[DocumentOutcome] = []
    for idx, (pdf_path, classification, extraction, error) in enumerate(ordered):
        if error or classification is None or extraction is None:
            outcomes.append(DocumentOutcome(
                filename=pdf_path.name,
                form_code="ERROR",
                classification_confidence=0.0,
                validation_status="FAIL",
                error=error or "Extraction failed",
            ))
            continue
        this_form_code = classification.form_code
        companion_codes = list(all_form_codes)
        companion_codes.remove(this_form_code)  # exclude this document's own code from the "companion" set
        outcomes.append(_validate_and_file(
            pdf_path, classification, extraction, report, progress_cb, channel,
            batch_form_codes=companion_codes, query_register=query_register,
        ))

    if profile:
        person_key = _person_key_for_batch(pdf_paths)
        source_files = [p.name for p in pdf_paths]
        report.add_consolidated_profile(profile, person_key=person_key, source_files=source_files)
        _emit(progress_cb, f"Consolidated profile for '{person_key}' added ({len(source_files)} document(s)).")
    else:
        person_key = _person_key_for_batch(pdf_paths)
        source_files = [p.name for p in pdf_paths]

    # If any document in this batch triggered a "companion document missing"
    # pre-validation flag (e.g. New Business with no KYC attached), flag the
    # whole batch in the top-level "Missing" folder so it's visible in the
    # filed folder tree, not just buried in the Excel report.
    missing_labels: list[str] = []
    for outcome in outcomes:
        for flag in outcome.prevalidation_flags or []:
            if flag.triggered and flag.flag_id in MISSING_DOCUMENT_FLAG_IDS:
                missing_labels.append(f"{outcome.filename}: {flag.label} — {flag.reason}")
    if missing_labels:
        flag_missing_documents(person_key, missing_labels, source_files=source_files)
        _emit(
            progress_cb,
            f"Batch '{person_key}' flagged as missing {len(missing_labels)} important "
            "document(s) — see the 'Missing' folder.",
        )

    return outcomes


def run_batch(
    intake_dir: Path | None = None,
    progress_cb: ProgressCallback = None,
    channel: str = "Unknown",
) -> tuple[list[DocumentOutcome], Path, Path]:
    """
    Processes every PDF in intake_dir (default: settings.paths.intake_dir).
    The folder can contain MULTIPLE PEOPLE's forms in one drop — files are
    first split into per-person groups (app.core.pipeline._group_by_person,
    using BIFM's "<FormType> - <Surname>.pdf" naming convention), and
    process_batch (BATCH = ONE PERSON) is run once per group. This is what
    stops a mixed folder from being backfilled/consolidated as if every
    document belonged to the same investor. Returns per-document outcomes
    across ALL people in stable input-file order, the path to the saved
    Excel report, and the path to the saved Query Register workbook
    (Section 4, Output 5 / Section 7 - Query Log + Recon sheets,
    automatically populated from this run's rejections and triggered
    pre-validation flags) — both reports cover every person in this run,
    one row/section per person, not one shared row for the whole folder.

    channel: "Email" or "Walk-in" — applied to every document in this run
    (the UI offers this as a per-batch choice, since submission channel is
    typically consistent within one intake drop).

    Documents are extracted concurrently (settings.max_workers, default 2)
    within each person's group. Each document spends most of its time
    waiting on the LLM HTTP call, so overlapping that wait with another
    document's page rendering (CPU-bound) cuts wall-clock time even
    without extra GPU/API capacity.
    """
    intake_dir = intake_dir or settings.paths.intake_dir
    pdf_files = sorted(intake_dir.glob("*.pdf"))

    if not pdf_files:
        _emit(progress_cb, f"No PDF files found in {intake_dir}")
        return (
            [],
            settings.paths.output_dir / settings.excel_report_name,
            settings.paths.output_dir / settings.query_register_report_name,
        )

    person_groups = _group_by_person(pdf_files)
    _emit(
        progress_cb,
        f"Found {len(pdf_files)} document(s) across {len(person_groups)} "
        f"person(s) to process (up to {settings.max_workers} in parallel per person).",
    )
    report = ExcelReportBuilder()
    query_register = QueryRegisterBuilder()

    outcomes: list[DocumentOutcome] = []
    for group in person_groups:
        person_key = _person_key_for_batch(group)
        _emit(progress_cb, f"--- Processing batch for '{person_key}' ({len(group)} document(s)) ---")
        outcomes.extend(process_batch(group, report, progress_cb, channel, query_register=query_register))

    output_path = report.save()
    query_register_path = query_register.save()
    _emit(progress_cb, f"Batch complete. Report saved to {output_path}")
    _emit(progress_cb, f"Query Register saved to {query_register_path}")
    return outcomes, output_path, query_register_path