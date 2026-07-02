"""
Module 4: Excel Report Generator — produces the consolidated workbook with the
4 sheets specified by the client: Investor Master, Beneficiary Details,
Validation Flags, Processing Log.

Key metadata columns in Investor Master (from the requirements doc):
  Form Type, Fund Name, Fund Category, Investor Entity Number, Investor Full Name,
  Instruction Date (signed), Processing Cut-off, Instruction Status.

FAIL rows are red, WARNING rows are amber, PASS rows are green.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models.schemas import ExtractionResult, FieldValue, ProcessingLogEntry, ValidationReport, ValidationStatus
from app.utils.confidence import needs_recheck
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

FILL_PASS    = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_WARNING = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
FILL_FAIL    = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
HEADER_FONT  = Font(bold=True, color="FFFFFF")
HEADER_FILL  = PatternFill(start_color="305496", end_color="305496", fill_type="solid")

# Styling for the "detail" row inserted beneath every data row, showing
# per-field confidence % and provenance (which document / page it was read
# from, or how a consolidated value was chosen).
DETAIL_FONT = Font(italic=True, size=9, color="6B6B6B")
DETAIL_FILL = PatternFill(start_color="F5F5F5", end_color="F5F5F5", fill_type="solid")


def _format_detail(fv, own_source_file: str | None = None) -> str:
    """
    Formats one FieldValue's confidence + provenance for a detail cell.

    `own_source_file` is the filename of the document the *row* itself
    represents (e.g. "DIS - AMOLEMO.pdf") - used so a plain on-document
    extraction still names the document it came from, not just "extracted",
    since the detail row is read on its own when the sheet is filtered/
    scrolled and shouldn't require cross-referencing the source_file column.
    """
    if fv is None:
        return ""
    bits = [f"{fv.confidence:.0f}% confidence"]
    if getattr(fv, "agreement", None):
        bits.append(f"{fv.agreement} — value taken from {fv.source}")
    elif fv.source == "extracted":
        doc = own_source_file or "this document"
        if fv.source_page:
            bits.append(f"extracted from {doc}, p.{fv.source_page}")
        else:
            bits.append(f"extracted from {doc}")
    elif fv.source.startswith("backfilled:"):
        bits.append(f"backfilled from {fv.source.split(':', 1)[1]}")
    else:
        bits.append(f"from {fv.source}")
    detail = " — ".join(bits)
    if needs_recheck(fv.confidence):
        detail += " — ⚠ recommend recheck"
    return detail

_STATUS_FILL = {
    ValidationStatus.PASS:    FILL_PASS,
    ValidationStatus.WARNING: FILL_WARNING,
    ValidationStatus.FAIL:    FILL_FAIL,
}

# Column order for the Investor Master sheet — mandatory metadata first,
# then extracted fields alphabetically.
_INVESTOR_MASTER_PRIORITY_COLS = [
    "source_file",
    "form_code",
    "form_type",           # derived: human-readable form name
    "entity_number",
    "full_name",
    "id_number",
    "id_type",
    "date_of_birth",
    "date_signed",
    "fund_name",
    "fund_number",
    "fund_category",       # derived: Money Market / Non-Money Market / GSGF
    "processing_cutoff",   # derived: 1:00 PM / 3:00 PM / Quarterly
    "fund_category_priority",  # derived: 1=MM (earliest cut-off, action first), 2=NMM, 3=GSGF
    "overall_validation_status",
    "instruction_status",  # Submitted / Captured / Approved / Rejected
    "rejection_reason",
    "channel",             # Email / Walk-in
    "upload_date",
    "captured_by",
    "authorized_by",
    "contact_number",
    "email",
    "citizenship",
    "account_type",
    "bank_account_name",
    "bank_name",
    "account_number",
    "branch_name",
    "branch_code",
    "account_type_banking",
]


# Column order for the Consolidated Investor Profile sheet - one row per
# batch/person, merging person-level fields picked up from any document in
# their batch (see app.services.consolidator).
_CONSOLIDATED_PROFILE_PRIORITY_COLS = [
    "person_key",
    "document_count",
    "source_documents",
    "entity_number",
    "entity_name",
    "full_name",
    "title",
    "id_number",
    "id_type",
    "id_expiry_date",
    "date_of_birth",
    "gender",
    "citizenship",
    "email",
    "contact_number",
    "residential_address",
    "postal_address",
    "occupation",
    "bank_account_name",
    "bank_name",
    "account_number",
    "branch_name",
    "branch_code",
    "account_type_banking",
    "authorized_signatory_name",
    "capacity",
]


def _write_header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    ws.freeze_panes = "A2"


def _autosize(ws: Worksheet, headers: list[str], min_width: int = 12, max_width: int = 45) -> None:
    for col_idx, header in enumerate(headers, start=1):
        longest = max(
            [len(str(header))] +
            [len(str(ws.cell(row=r, column=col_idx).value or "")) for r in range(2, ws.max_row + 1)]
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(longest + 2, min_width), max_width)


def _ordered_investor_columns(all_rows: list[dict]) -> list[str]:
    """Return columns with priority cols first, then remaining alphabetically."""
    all_keys = {k for row in all_rows for k in row.keys()}
    priority = [c for c in _INVESTOR_MASTER_PRIORITY_COLS if c in all_keys]
    rest = sorted(all_keys - set(priority))
    return priority + rest


class ExcelReportBuilder:
    """
    Accumulates results across a batch run, then writes the consolidated workbook once.
    Call add_form(...) per processed document, then save().
    """

    def __init__(self) -> None:
        self._investor_rows: list[dict] = []
        self._investor_field_values: list[dict[str, FieldValue]] = []
        self._beneficiary_rows: list[dict] = []
        self._validation_rows: list[tuple[dict, ValidationStatus]] = []
        self._log_entries: list[ProcessingLogEntry] = []
        self._consolidated_rows: list[dict] = []
        self._consolidated_field_values: list[dict[str, FieldValue]] = []

    def add_form(
        self,
        extraction: ExtractionResult,
        validation: ValidationReport,
        log_entry: ProcessingLogEntry,
    ) -> None:
        flat = extraction.to_flat_dict()
        flat["entity_number"] = validation.entity_number
        flat["overall_validation_status"] = validation.overall_status.value
        # Workflow/audit metadata (Metadata Fields for Filing table) lives on
        # the ProcessingLogEntry since it's determined at filing time, but the
        # client expects to see it alongside the extracted data in Investor
        # Master too, not buried only in the Processing Log sheet.
        flat["instruction_status"] = log_entry.instruction_status
        flat["rejection_reason"] = log_entry.rejection_reason
        flat["channel"] = log_entry.channel
        flat["upload_date"] = log_entry.upload_date
        flat["captured_by"] = log_entry.captured_by
        flat["authorized_by"] = log_entry.authorized_by
        self._investor_rows.append(flat)
        # Keep the underlying FieldValues (confidence, source) alongside the
        # flat row so the detail row can be rendered beneath it.
        self._investor_field_values.append(dict(extraction.fields))

        for b in extraction.beneficiaries:
            self._beneficiary_rows.append({
                "entity_number":    validation.entity_number,
                "source_file":      extraction.source_file,
                "beneficiary_name": b.name,
                "relationship":     b.relationship,
                "split_percent":    b.split_percent,
            })

        for r in validation.results:
            self._validation_rows.append((
                {
                    "entity_number": validation.entity_number,
                    "source_file":   extraction.source_file,
                    "form_code":     extraction.form_code,
                    "field":         r.field_id,
                    "value":         r.value,
                    "status":        r.status.value,
                    "message":       r.message,
                },
                r.status,
            ))

        self._log_entries.append(log_entry)

    def add_consolidated_profile(
        self,
        profile: dict[str, FieldValue],
        person_key: str,
        source_files: list[str],
    ) -> None:
        """
        Records one row for the "Consolidated Investor Profile" sheet: the
        merged person-level fields (entity_number, full_name, contact
        details, banking details, etc.) picked up from ACROSS every
        document in this person's batch — not just whichever single form
        happened to have that field filled in. One call per batch/person.
        """
        flat = {fid: fv.value for fid, fv in profile.items()}
        flat["person_key"] = person_key
        flat["document_count"] = len(source_files)
        flat["source_documents"] = ", ".join(source_files)
        self._consolidated_rows.append(flat)
        self._consolidated_field_values.append(dict(profile))

    def save(self, output_path: Path | None = None) -> Path:
        output_path = output_path or (settings.paths.output_dir / settings.excel_report_name)
        wb = Workbook()
        wb.remove(wb.active)

        self._write_consolidated_profile(wb)
        self._write_investor_master(wb)
        self._write_beneficiary_details(wb)
        self._write_validation_flags(wb)
        self._write_processing_log(wb)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        logger.info("Saved consolidated Excel report to %s", output_path)
        return output_path

    def _write_consolidated_profile(self, wb: Workbook) -> None:
        """
        One row per batch/person — the merged view an Ops reviewer actually
        wants: "who is this, and what do we know about them from everything
        they handed in", rather than having to cross-reference 4 separate
        Investor Master rows to piece together one person's contact and
        banking details.
        """
        ws = wb.create_sheet("Consolidated Investor Profile", 0)
        if not self._consolidated_rows:
            _write_header(ws, ["No data"])
            return
        all_keys = {k for row in self._consolidated_rows for k in row.keys()}
        priority = [c for c in _CONSOLIDATED_PROFILE_PRIORITY_COLS if c in all_keys]
        rest = sorted(all_keys - set(priority))
        headers = priority + rest
        _write_header(ws, headers)
        for row, field_values in zip(self._consolidated_rows, self._consolidated_field_values):
            ws.append([row.get(h, "") for h in headers])
            ws.append([_format_detail(field_values.get(h)) for h in headers])
            detail_row_idx = ws.max_row
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=detail_row_idx, column=col_idx)
                cell.font = DETAIL_FONT
                cell.fill = DETAIL_FILL
        _autosize(ws, headers)

    def _write_investor_master(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Investor Master")
        if not self._investor_rows:
            _write_header(ws, ["No data"])
            return
        headers = _ordered_investor_columns(self._investor_rows)

        # MM has the earliest same-day cut-off (1PM, vs NMM's 3PM and GSGF's
        # quarterly-only schedule), so MM instructions must be actioned
        # first. Sort rows by fund_category_priority ascending (1=MM) so
        # the most time-sensitive instructions surface at the top of the
        # sheet; forms with no fund at all (APPFORM/STATIC/KYC) have no
        # priority value and sort after every fund-bearing row. Ties keep
        # their original (stable) order.
        def _priority_key(row: dict) -> int:
            value = row.get("fund_category_priority")
            try:
                return int(value)
            except (TypeError, ValueError):
                return 99

        # Keep each row's FieldValues attached through the sort so the detail
        # row written underneath stays paired with the correct data row.
        paired = sorted(
            zip(self._investor_rows, self._investor_field_values),
            key=lambda pair: _priority_key(pair[0]),
        )

        _write_header(ws, headers)
        status_col_idx = headers.index("overall_validation_status") + 1 if "overall_validation_status" in headers else None
        for row, field_values in paired:
            ws.append([row.get(h, "") for h in headers])
            data_row_idx = ws.max_row

            # Color-code the data row by overall validation status.
            if status_col_idx:
                status_val = ws.cell(row=data_row_idx, column=status_col_idx).value
                fill = None
                if status_val == "PASS":
                    fill = FILL_PASS
                elif status_val == "WARNING":
                    fill = FILL_WARNING
                elif status_val in ("FAIL", "ERROR"):
                    fill = FILL_FAIL
                if fill:
                    for col_idx in range(1, len(headers) + 1):
                        ws.cell(row=data_row_idx, column=col_idx).fill = fill

            # Detail row: confidence % + provenance (source document/page)
            # for each extracted field. Metadata columns (source_file,
            # form_type, derived columns, workflow fields, ...) have no
            # FieldValue, so they're left blank.
            own_source_file = row.get("source_file")
            ws.append([_format_detail(field_values.get(h), own_source_file) for h in headers])
            detail_row_idx = ws.max_row
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=detail_row_idx, column=col_idx)
                cell.font = DETAIL_FONT
                cell.fill = DETAIL_FILL

        _autosize(ws, headers)

    def _write_beneficiary_details(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Beneficiary Details")
        headers = ["entity_number", "source_file", "beneficiary_name", "relationship", "split_percent"]
        _write_header(ws, headers)
        for row in self._beneficiary_rows:
            ws.append([row.get(h, "") for h in headers])
        _autosize(ws, headers)

    def _write_validation_flags(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Validation Flags")
        headers = ["entity_number", "source_file", "form_code", "field", "value", "status", "message"]
        _write_header(ws, headers)
        for row, status in self._validation_rows:
            ws.append([row.get(h, "") for h in headers])
            fill = _STATUS_FILL.get(status)
            if fill:
                for col_idx in range(1, len(headers) + 1):
                    ws.cell(row=ws.max_row, column=col_idx).fill = fill
        _autosize(ws, headers)

    def _write_processing_log(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Processing Log")
        headers = [
            "timestamp", "original_filename", "new_filename",
            "form_type_detected", "classification_confidence",
            "validation_status", "instruction_status", "rejection_reason",
            "channel", "upload_date", "destination_path",
            "captured_by", "authorized_by", "processor_id",
        ]
        _write_header(ws, headers)
        for entry in self._log_entries:
            ws.append([getattr(entry, h, "") for h in headers])
        _autosize(ws, headers)
