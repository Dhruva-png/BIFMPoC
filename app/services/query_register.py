"""
Module 6: Query Register Automation (Section 4, Output 5 / Section 7 of the
understanding document).

Produces a workbook matching the structure of BIFM's own Query Register
Excel file, which has two sheets:

  Sheet 1 - Query Log: the tracker the Sales / Contact Center team uses to
  log queries or issues raised by clients or internally, and track their
  resolution. Columns (per the shared file): No., Date, Type of Enquiry,
  Client Name, Query Description, Logged Via, Registered By, Assigned To,
  Checked By, Resolution Progress, Date Submitted to Ops / Resolved,
  Date Captured, Status, Investor Portal Request, No. of Days Open,
  Sales Comments to Ops, Ops Resolution, Ops Resolution Date.

  Sheet 2 - Recon: a daily / periodic reconciliation tracker with counts of
  instructions Received / Processed / Pending, broken down by instruction
  type: New Business, Additional Investments, Withdrawals, Debit Orders,
  Switch/Transfer, and Static.

This is populated automatically from each batch run instead of being
hand-maintained by the Sales/Contact Center team, per Section 4 Output 5:
"Automatically log queried, rejected, or incomplete instructions into the
Query Register format shared by BIFM ... reducing manual entry by the
Sales/Contact Center team and keeping the recon counts current."

A "query" line is logged for:
  - Any instruction that came back REJECTED from validation (Section 3,
    Step 5's rejection flow) - e.g. wrong banking details.
  - Any triggered pre-validation flag (Section 3, Step 2's missing/
    incomplete documentation flow, plus Section 8's other rejection-risk
    signals), whether or not the instruction ultimately validated PASS/
    WARNING - a triggered flag is itself something Sales needs to follow
    up with the client about.
Clean straight-through instructions with no rejection and no triggered
flags don't get a Query Log row at all - only the Recon counts move.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models.schemas import ExtractionResult, PreValidationFlag, ProcessingLogEntry, ValidationReport
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
FILL_HIGH_RISK = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_MEDIUM_RISK = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

QUERY_LOG_HEADERS = [
    "No.", "Date", "Type of Enquiry", "Client Name", "Query Description",
    "Logged Via", "Registered By", "Assigned To", "Checked By",
    "Resolution Progress", "Date Submitted to Ops / Resolved", "Date Captured",
    "Status", "Investor Portal Request", "No. of Days Open",
    "Sales Comments to Ops", "Ops Resolution", "Ops Resolution Date",
]

RECON_HEADERS = ["Instruction Type", "Received", "Processed", "Pending"]

# Order matches Section 7, Sheet 2 - Recon exactly.
RECON_INSTRUCTION_TYPES = [
    "New Business",
    "Additional Investments",
    "Withdrawals",
    "Debit Orders",
    "Switch/Transfer",
    "Static",
]

# Maps this app's form codes (config/form_types.json) -> the Recon sheet's
# instruction-type buckets. DIS and DIS_GSG both roll up to "Withdrawals"
# for recon purposes even though they're tracked separately everywhere
# else in this app (different cut-offs / lock-ins - see form_types.json).
# KYC is a companion document (Section 8), not its own instruction line.
# SWITCH is included for forward-compatibility with Output 1's
# classification list even though it isn't one of the six sample form
# types extracted in this POC (Section 5).
_FORM_CODE_TO_RECON_TYPE: dict[str, str | None] = {
    "APPFORM": "New Business",
    "ADD": "Additional Investments",
    "DIS": "Withdrawals",
    "DIS_GSG": "Withdrawals",
    "DEBIT": "Debit Orders",
    "STATIC": "Static",
    "SWITCH": "Switch/Transfer",
    "KYC": None,
}


def recon_type_for_form_code(form_code: str) -> str | None:
    return _FORM_CODE_TO_RECON_TYPE.get(form_code)


def _write_header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    ws.freeze_panes = "A2"


def _autosize(ws: Worksheet, headers: list[str], min_width: int = 10, max_width: int = 45) -> None:
    for col_idx, header in enumerate(headers, start=1):
        longest = max(
            [len(str(header))]
            + [len(str(ws.cell(row=r, column=col_idx).value or "")) for r in range(2, ws.max_row + 1)]
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(longest + 2, min_width), max_width)


class QueryRegisterBuilder:
    """
    Accumulates queried/rejected/incomplete instructions across a batch
    run, plus Received/Processed/Pending counts by instruction type, then
    writes the two-sheet workbook once. Mirrors the add_form(...) / save()
    pattern of app.services.report_generator.ExcelReportBuilder so the two
    can be filled in from the same call site in the pipeline.
    """

    def __init__(self) -> None:
        self._query_rows: list[tuple[dict, str]] = []  # (row, rejection_risk) for colour-coding
        self._counter = 0
        self._recon_counts: dict[str, dict[str, int]] = {
            t: {"Received": 0, "Processed": 0, "Pending": 0} for t in RECON_INSTRUCTION_TYPES
        }

    def add_form(
        self,
        extraction: ExtractionResult,
        validation: ValidationReport,
        log_entry: ProcessingLogEntry,
        prevalidation_flags: list[PreValidationFlag] | None = None,
    ) -> None:
        """One call per processed document - same call site as
        ExcelReportBuilder.add_form in app.core.pipeline."""
        recon_type = recon_type_for_form_code(extraction.form_code)
        if recon_type:
            counts = self._recon_counts[recon_type]
            counts["Received"] += 1
            if log_entry.instruction_status in ("Captured", "Approved"):
                counts["Processed"] += 1
            else:
                # Rejected or still Submitted - sits with Sales/Ops until
                # resolved, so it counts as Pending for recon purposes.
                counts["Pending"] += 1

        triggered = [f for f in (prevalidation_flags or []) if f.triggered]
        is_rejected = log_entry.instruction_status == "Rejected"

        if not is_rejected and not triggered:
            return  # clean straight-through instruction - nothing to log

        full_name = (
            extraction.field_value("full_name")
            or extraction.field_value("entity_number")
            or "Unknown"
        )
        today = date.today().isoformat()

        if is_rejected:
            self._add_row(
                enquiry_type="Rejected Instruction",
                client_name=str(full_name),
                description=log_entry.rejection_reason or "Validation failed",
                status="Rejected - Pending Correction",
                rejection_risk="High",
                today=today,
                log_entry=log_entry,
            )

        for flag in triggered:
            label_l = flag.label.lower()
            reason_l = (flag.reason or "").lower()
            enquiry_type = (
                "Incomplete / Missing Documentation"
                if ("missing" in label_l or "companion" in reason_l or "kyc" in label_l)
                else "Pre-Validation Query"
            )
            self._add_row(
                enquiry_type=enquiry_type,
                client_name=str(full_name),
                description=f"{flag.label}: {flag.reason}" if flag.reason else flag.label,
                status="Query Raised - Pending Follow-up",
                rejection_risk=flag.rejection_risk,
                today=today,
                log_entry=log_entry,
            )

    def _add_row(
        self, *, enquiry_type: str, client_name: str, description: str, status: str,
        rejection_risk: str, today: str, log_entry: ProcessingLogEntry,
    ) -> None:
        self._counter += 1
        row = {
            "No.": self._counter,
            "Date": today,
            "Type of Enquiry": enquiry_type,
            "Client Name": client_name,
            "Query Description": description,
            "Logged Via": log_entry.channel,
            "Registered By": log_entry.captured_by,
            "Assigned To": "",
            "Checked By": "",
            "Resolution Progress": "Open",
            "Date Submitted to Ops / Resolved": "",
            "Date Captured": log_entry.upload_date,
            "Status": status,
            "Investor Portal Request": "No",
            "No. of Days Open": 0,
            "Sales Comments to Ops": "",
            "Ops Resolution": "",
            "Ops Resolution Date": "",
        }
        self._query_rows.append((row, rejection_risk))

    def save(self, output_path: Path | None = None) -> Path:
        output_path = output_path or (settings.paths.output_dir / settings.query_register_report_name)
        wb = Workbook()
        wb.remove(wb.active)
        self._write_query_log(wb)
        self._write_recon(wb)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        logger.info("Saved Query Register to %s", output_path)
        return output_path

    def _write_query_log(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Query Log")
        _write_header(ws, QUERY_LOG_HEADERS)
        for row, risk in self._query_rows:
            ws.append([row.get(h, "") for h in QUERY_LOG_HEADERS])
            risk_l = str(risk).lower()
            fill = FILL_HIGH_RISK if risk_l.startswith("high") else (
                FILL_MEDIUM_RISK if risk_l.startswith("medium") else None
            )
            if fill:
                for col_idx in range(1, len(QUERY_LOG_HEADERS) + 1):
                    ws.cell(row=ws.max_row, column=col_idx).fill = fill
        if not self._query_rows:
            ws.append(["(No queries, rejections, or missing-documentation flags this run)"])
        _autosize(ws, QUERY_LOG_HEADERS)

    def _write_recon(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Recon")
        _write_header(ws, RECON_HEADERS)
        for instr_type in RECON_INSTRUCTION_TYPES:
            counts = self._recon_counts[instr_type]
            ws.append([instr_type, counts["Received"], counts["Processed"], counts["Pending"]])
        total_row = ["TOTAL"] + [
            sum(self._recon_counts[t][col] for t in RECON_INSTRUCTION_TYPES)
            for col in ("Received", "Processed", "Pending")
        ]
        ws.append(total_row)
        for col_idx in range(1, len(RECON_HEADERS) + 1):
            ws.cell(row=ws.max_row, column=col_idx).font = Font(bold=True)
        _autosize(ws, RECON_HEADERS)
