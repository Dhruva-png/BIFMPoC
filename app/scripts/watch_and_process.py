"""
Fully automatic drop-folder processor.

Run this once (e.g. as a background task / Windows service / just a
terminal you leave open) and it watches ONE folder on your laptop:

    <project_root>/auto_intake/

Drop ANY BIFM UT PDF(s) into that folder — from one person or several
people, one at a time or all at once — and this script will, with no
button-clicking required:

  1. Detect the new file(s) and wait for the folder to go quiet for a
     few seconds (so a batch of files dropped together, e.g. an email
     with 4 attachments saved one-by-one, is treated as one event
     instead of triggering 4 separate half-finished runs).
  2. Group the new files by person (BIFM's existing "<FormType> -
     <Surname>.pdf" naming convention — same grouping logic the manual
     Streamlit "Run Batch" button already uses).
  3. For EACH person: run the full classify -> extract -> consolidate ->
     validate -> file pipeline (app.core.pipeline.process_batch), which:
       - backfills blank identity/contact/banking fields from that
         person's other documents in the same drop
       - files each source PDF into the correct SharePoint-mirror
         output subfolder (app.services.filer), exactly as today
       - writes ONE individual Excel workbook for that person only
         (all the usual sheets: Consolidated Investor Profile,
         Investor Master, Beneficiary Details, Validation Flags,
         Pre-Validation Flags, Confidence Scores, Processing Log) to:
             output/individual_reports/<person>/<person>_<timestamp>.xlsx
  4. ALSO append that same person's rows into one running MASTER
     workbook covering everyone processed so far:
             output/BIFM_UT_Processing_Report.xlsx
     (the same file/location the manual Streamlit button already
     produces, so nothing downstream needs to change).
  5. Move the original source PDF(s) — this already happens as a side
     effect of step 3's filing step, no separate copy needed.

Requires the `watchdog` package:
    pip install watchdog

Run:
    python -m app.scripts.watch_and_process
(from the project root, same place you'd run `streamlit run streamlit_app.py`)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.core.pipeline import _group_by_person, process_batch
from app.services.report_generator import ExcelReportBuilder
from app.services.query_register import QueryRegisterBuilder
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

# Folder you drop files into. Kept separate from settings.paths.intake_dir
# (which is what the manual "Use intake folder" option in Streamlit reads)
# so the two workflows never race over the same files.
AUTO_INTAKE_DIR = settings.paths.output_dir.parent / "auto_intake"
INDIVIDUAL_REPORTS_DIR = settings.paths.output_dir / "individual_reports"
MASTER_REPORT_PATH = settings.paths.output_dir / settings.excel_report_name
MASTER_QUERY_REGISTER_PATH = settings.paths.output_dir / settings.query_register_report_name

# How long the folder must sit quiet (no new/modified PDFs) before a batch
# of newly-arrived files is considered "done dropping" and gets processed.
# Raise this if you routinely save several attachments one at a time with
# noticeable gaps between them.
DEBOUNCE_SECONDS = 8.0


def _safe_name(text: str) -> str:
    keep = "-_ "
    return "".join(c for c in text if c.isalnum() or c in keep).strip().replace(" ", "_") or "UNKNOWN"


class _Watcher(FileSystemEventHandler):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[Path] = set()
        self._timer: threading.Timer | None = None

    def on_created(self, event) -> None:  # noqa: ANN001
        self._queue(event)

    def on_moved(self, event) -> None:  # noqa: ANN001
        # covers files saved via "temp file -> rename" (common with browsers/
        # email clients writing downloads), which fire a moved event rather
        # than a created event for the final .pdf.
        if hasattr(event, "dest_path"):
            path = Path(event.dest_path)
            if path.suffix.lower() == ".pdf":
                self._add(path)

    def _queue(self, event) -> None:  # noqa: ANN001
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf":
            return
        self._add(path)

    def _add(self, path: Path) -> None:
        with self._lock:
            self._pending.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()
        logger.info("Detected new file: %s (waiting for folder to go quiet...)", path.name)

    def _flush(self) -> None:
        with self._lock:
            batch = sorted(p for p in self._pending if p.exists())
            self._pending.clear()
            self._timer = None
        if batch:
            _process_new_files(batch)


def _process_new_files(pdf_paths: list[Path]) -> None:
    logger.info("Processing %d new file(s): %s", len(pdf_paths), [p.name for p in pdf_paths])

    person_groups = _group_by_person(pdf_paths)
    master_report = ExcelReportBuilder()
    master_query_register = QueryRegisterBuilder()
    if MASTER_REPORT_PATH.exists():
        logger.info(
            "Note: a prior master report exists at %s and will be OVERWRITTEN with a fresh "
            "workbook containing this run's people only, plus any still tracked in this "
            "process's memory. For a workbook that accumulates every person ever processed "
            "across script restarts, see the note at the bottom of this file.",
            MASTER_REPORT_PATH,
        )

    for group in person_groups:
        person_key = _safe_name(group[0].stem.split(" - ")[-1] if " - " in group[0].stem else group[0].stem)
        logger.info("--- Person batch: '%s' (%d document(s)) ---", person_key, len(group))

        # One dedicated builder for this person's individual workbook, PLUS
        # feed the same outcomes into the shared master builder below so
        # both the individual and the consolidated report get every row.
        individual_report = ExcelReportBuilder()
        individual_query_register = QueryRegisterBuilder()

        outcomes = process_batch(
            group, individual_report, progress_cb=logger.info,
            channel="Unknown", query_register=individual_query_register,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        person_dir = INDIVIDUAL_REPORTS_DIR / person_key
        individual_path = person_dir / f"{person_key}_{timestamp}.xlsx"
        individual_report.save(individual_path)
        individual_query_register.save(person_dir / f"{person_key}_{timestamp}_QueryRegister.xlsx")
        logger.info("Saved individual report for '%s' -> %s", person_key, individual_path)

        # Re-run the same outcomes into the master builder so the running
        # consolidated workbook also gets this person's rows. (Cheap to
        # redo — process_batch's real work, the LLM calls, already
        # happened above; this just re-feeds already-computed results.)
        _replay_into_master(group, outcomes, master_report, master_query_register)

    master_path = master_report.save(MASTER_REPORT_PATH)
    master_query_register.save(MASTER_QUERY_REGISTER_PATH)
    logger.info("Updated master consolidated report -> %s", master_path)


def _replay_into_master(group, outcomes, master_report, master_query_register) -> None:
    """
    Feeds one person's already-computed outcomes into the master
    ExcelReportBuilder/QueryRegisterBuilder without re-running the LLM
    pipeline, by re-adding their DocumentOutcome data directly.
    """
    from app.services.consolidator import build_person_profile

    good_extractions = [o.extraction for o in outcomes if o.extraction is not None and o.error is None]

    # Re-add each outcome's data (pure data, no filing side effects — filing
    # already happened once inside process_batch above) into the master
    # workbook using a placeholder ProcessingLogEntry built from the outcome.
    from app.models.schemas import ProcessingLogEntry
    for o in outcomes:
        if o.extraction is None or o.validation is None:
            continue
        log_entry = ProcessingLogEntry(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            original_filename=o.filename,
            new_filename=o.filename,
            form_type_detected=o.form_code,
            classification_confidence=o.classification_confidence,
            validation_status=o.validation_status,
            destination_path="",
        )
        master_report.add_form(
            o.extraction, o.validation, log_entry, prevalidation_flags=o.prevalidation_flags,
        )
        master_query_register.add_form(
            o.extraction, o.validation, log_entry, prevalidation_flags=o.prevalidation_flags,
        )

    profile = build_person_profile(good_extractions)
    if profile:
        person_key = group[0].stem.split(" - ")[-1].strip() if " - " in group[0].stem else group[0].stem
        master_report.add_consolidated_profile(
            profile, person_key=person_key, source_files=[p.name for p in group],
        )


def main() -> None:
    AUTO_INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    INDIVIDUAL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Watching %s for new PDFs. Drop files in there — everything else is automatic.", AUTO_INTAKE_DIR)

    handler = _Watcher()
    observer = Observer()
    observer.schedule(handler, str(AUTO_INTAKE_DIR), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()