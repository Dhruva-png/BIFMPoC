"""
Module 4: Excel Report Generator (Section 5 / 7.2).

Produces the consolidated workbook with exactly the 4 sheets specified:
Investor Master, Beneficiary Details, Validation Flags, Processing Log.
FAIL rows in red, WARNING in amber, PASS in green (Section 5.2).
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models.schemas import ExtractionResult, ProcessingLogEntry, ValidationReport, ValidationStatus
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

FILL_PASS = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_WARNING = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
FILL_FAIL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")

_STATUS_FILL = {
    ValidationStatus.PASS: FILL_PASS,
    ValidationStatus.WARNING: FILL_WARNING,
    ValidationStatus.FAIL: FILL_FAIL,
}


def _write_header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    ws.freeze_panes = "A2"


def _autosize(ws: Worksheet, headers: list[str], min_width: int = 12, max_width: int = 45) -> None:
    for col_idx, header in enumerate(headers, start=1):
        longest = max(
            [len(str(header))] + [len(str(ws.cell(row=r, column=col_idx).value or "")) for r in range(2, ws.max_row + 1)]
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(longest + 2, min_width), max_width)


class ExcelReportBuilder:
    """
    Accumulates results across a batch run, then writes the consolidated workbook once.
    Call add_form(...) per processed document, then save().
    """

    def __init__(self) -> None:
        self._investor_rows: list[dict] = []
        self._beneficiary_rows: list[dict] = []
        self._validation_rows: list[tuple[dict, ValidationStatus]] = []
        self._log_entries: list[ProcessingLogEntry] = []

    def add_form(self, extraction: ExtractionResult, validation: ValidationReport, log_entry: ProcessingLogEntry) -> None:
        flat = extraction.to_flat_dict()
        flat["entity_number"] = validation.entity_number
        flat["overall_validation_status"] = validation.overall_status.value
        self._investor_rows.append(flat)

        for b in extraction.beneficiaries:
            self._beneficiary_rows.append(
                {
                    "entity_number": validation.entity_number,
                    "source_file": extraction.source_file,
                    "beneficiary_name": b.name,
                    "relationship": b.relationship,
                    "split_percent": b.split_percent,
                }
            )

        for r in validation.results:
            self._validation_rows.append(
                (
                    {
                        "entity_number": validation.entity_number,
                        "source_file": extraction.source_file,
                        "field": r.field_id,
                        "value": r.value,
                        "status": r.status.value,
                        "message": r.message,
                    },
                    r.status,
                )
            )

        self._log_entries.append(log_entry)

    def save(self, output_path: Path | None = None) -> Path:
        output_path = output_path or (settings.paths.output_dir / settings.excel_report_name)
        wb = Workbook()
        wb.remove(wb.active)

        self._write_investor_master(wb)
        self._write_beneficiary_details(wb)
        self._write_validation_flags(wb)
        self._write_processing_log(wb)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        logger.info("Saved consolidated Excel report to %s", output_path)
        return output_path

    def _write_investor_master(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Investor Master")
        if not self._investor_rows:
            _write_header(ws, ["No data"])
            return
        headers = sorted({k for row in self._investor_rows for k in row.keys()})
        _write_header(ws, headers)
        for row in self._investor_rows:
            ws.append([row.get(h, "") for h in headers])
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
        headers = ["entity_number", "source_file", "field", "value", "status", "message"]
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
        headers = ["timestamp", "original_filename", "new_filename", "form_type_detected",
                   "classification_confidence", "validation_status", "destination_path", "processor_id"]
        _write_header(ws, headers)
        for entry in self._log_entries:
            ws.append([getattr(entry, h) for h in headers])
        _autosize(ws, headers)
