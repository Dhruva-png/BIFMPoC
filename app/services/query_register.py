"""
Module 6: Query Register Automation (Section 4, Output 5 / Section 7 of the
understanding document).

Produces a workbook matching BIFM's own Query Register Excel file EXACTLY,
including a structural quirk that isn't obvious from just looking at it:
five of the visible columns (Type of Enquiry, Logged Via, Registered by,
Status, Investor Portal Request) each have a second, header-less column
sitting next to them (D, H, J, Q, S respectively) that is NOT a data field
- it's the dropdown picklist source for the real column, referenced by
Excel's own "list" data validation. This module writes those lists once
near the top and wires up matching dropdowns, exactly like BIFM's file.

Sheet 1 - Query Log (23 physical columns):
  A No.                                  N Date Submitted to Ops / Resolved
  B Date                                 O Date Captured
  C Type of Enquiry   (dropdown)         P Status              (dropdown)
  D   [picklist: enquiry types]          Q   [picklist: Open/Resolved]
  E Client Name                          R Investor Portal Req (dropdown)
  F Query Description                    S   [picklist: Yes/No/Pending/N/A]
  G Logged Via        (dropdown)         T No. of Days Open
  H   [picklist: logged-via channels]    U Sales Comments to Ops  \
  I Registered by      (dropdown)        V Ops Resolution          } merged
  J   [picklist: staff names]            W Ops Resolution Date    / "Operations
  K Assigned to                                                     Use ONLY"
  L Checked by                                                      banner,
  M Resolution Progress                                             row 1

Sheet 2 - Recon: daily/periodic reconciliation tracker (Received /
Processed / Pending) by instruction type, columns B-E to match BIFM's own
layout, which starts one column in from the sheet edge.

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

Every row this module writes gets Status = "Open" - matching BIFM's own
convention that new entries start open and Sales/Ops flips them to
"Resolved" by hand once actioned. Descriptive detail about WHY something
is open (e.g. "Awaiting corrected form from client") goes in Resolution
Progress, not Status - Status is strictly the 2-value Open/Resolved
picklist BIFM's file uses.
"""
from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from app.models.schemas import ExtractionResult, PreValidationFlag, ProcessingLogEntry, ValidationReport
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
THIN_BORDER = Border(*(Side(style="thin"),) * 4)

FILL_HIGH_RISK = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_MEDIUM_RISK = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

# "Operations Use ONLY" banner - matches BIFM's own file: large, bold, red,
# merged across the 3 Ops-only columns in row 1.
OPS_BANNER_FONT = Font(bold=True, size=16, color="FF0000")
OPS_BANNER_FILL = PatternFill(start_color="FDE9D9", end_color="FDE9D9", fill_type="solid")

DATE_FORMAT = "dd/mm/yyyy"

# How many rows past the current data to keep the dropdown validations
# active for, so Sales/Ops can keep adding rows by hand (in Excel
# directly) with the picklists still working, same as BIFM's own template
# pre-applying validation across ~1700 rows in advance.
VALIDATION_ROW_BUFFER = 500

# ---------------------------------------------------------------------------
# Column layout - position (1-indexed) : header text (None = picklist-only,
# no visible header, matching BIFM's own file exactly).
# ---------------------------------------------------------------------------
_COLUMNS: list[tuple[str, str | None]] = [
    ("No.", "No."),
    ("Date", "Date"),
    ("Type of Enquiry", "Type of Enquiry"),
    ("_enquiry_type_list", None),
    ("Client Name", "Client Name"),
    ("Query Description", "Query Description"),
    ("Logged Via", "Logged Via"),
    ("_logged_via_list", None),
    ("Registered by", "Registered by"),
    ("_registered_by_list", None),
    ("Assigned to", "Assigned to"),
    ("Checked by", "Checked by"),
    ("Resolution Progress", "Resolution Progress"),
    ("Date Submitted to Ops / Resolved", "Date Submitted to Ops / Resolved"),
    ("Date Captured", "Date Captured"),
    ("Status", "Status"),
    ("_status_list", None),
    ("Investor Portal Request", "Investor Portal Request"),
    ("_portal_request_list", None),
    ("No. of Days Open", "No. of Days Open"),
    ("Sales Comments to Ops", "Sales Comments to Ops"),
    ("Ops Resolution", "Ops Resolution"),
    ("Ops Resolution Date", "Ops Resolution Date"),
]
_COL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVW"
_COL_INDEX = {key: i + 1 for i, (key, _) in enumerate(_COLUMNS)}  # 1-indexed

_COLUMN_WIDTHS = {
    "A": 7, "B": 12, "C": 30, "D": 26, "E": 26, "F": 38, "G": 15, "H": 24,
    "I": 14, "J": 18, "K": 18, "L": 15, "M": 30, "N": 14, "O": 14, "P": 12,
    "Q": 14, "R": 20, "S": 24, "T": 13, "U": 28, "V": 32, "W": 22,
}

# ---------------------------------------------------------------------------
# Dropdown picklists - written once as static reference data (rows 3+ in
# their respective columns), exactly matching BIFM's own file. These are
# NOT per-row data - they're what the dropdown on the real column offers.
# ---------------------------------------------------------------------------
ENQUIRY_TYPE_OPTIONS = [
    "Transaction status", "Rejected instruction", "Delayed processing",
    "Balance/valuation enquiry", "Statement request", "Account details change",
    "KYC/FICA query", "Documentation request", "Fund performance query",
    "Product/fee query", "Complaint", "Portal/access issue",
    "Income distribution query", "General enquiry",
]
LOGGED_VIA_OPTIONS = ["Email", "Telephone", "Walk-in", "Investor Portal", "Fax", "Letter"]
REGISTERED_BY_OPTIONS = [
    "Thato Kgasa", "Gorata Nkele", "Kesego", "Onkemetse",
    "Contact Center Agent 1", "Contact Center Agent 2",
]
STATUS_OPTIONS = ["Open", "Resolved"]
PORTAL_REQUEST_OPTIONS = ["Yes", "No", "Pending", "N/A"]

RECON_HEADERS = ["Instruction Type", "Received", "Processed", "Pending"]

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


def _map_enquiry_type(is_rejected: bool, flag_label: str = "", flag_reason: str = "") -> str:
    """Maps our internal rejection/flag signal onto BIFM's actual 14-item
    Type of Enquiry picklist, instead of inventing new category labels
    that wouldn't appear in their dropdown. Falls back to "General
    enquiry" (itself a valid list option) when nothing more specific
    matches - the free-text Query Description column still carries the
    full detail regardless of which category tag is picked here."""
    if is_rejected:
        return "Rejected instruction"
    text = f"{flag_label} {flag_reason}".lower()
    if "kyc" in text or "fica" in text:
        return "KYC/FICA query"
    if "missing" in text or "companion" in text or "document" in text:
        return "Documentation request"
    return "General enquiry"


def _portal_request_value(channel: str) -> str:
    return "Yes" if str(channel).strip().lower() == "investor portal" else "No"


class QueryRegisterBuilder:
    """
    Accumulates queried/rejected/incomplete instructions across a batch
    run, plus Received/Processed/Pending counts by instruction type, then
    writes the two-sheet workbook once. Mirrors the add_form(...) / save()
    pattern of app.services.report_generator.ExcelReportBuilder so the two
    can be filled in from the same call site in the pipeline.

    One instance is shared across every document in a run, including
    documents from DIFFERENT people's batches processed concurrently by
    app.core.pipeline.run_batch - unlike ExcelReportBuilder (whose state is
    plain list.append calls, atomic under the GIL), self._counter and
    self._recon_counts are read-modify-write, so add_form is guarded by
    self._lock to stop two threads' calls from racing and silently losing
    an increment (duplicate "No." values, undercounted recon figures).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
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
        with self._lock:
            self._add_form_locked(extraction, validation, log_entry, prevalidation_flags)

    def _add_form_locked(
        self,
        extraction: ExtractionResult,
        validation: ValidationReport,
        log_entry: ProcessingLogEntry,
        prevalidation_flags: list[PreValidationFlag] | None = None,
    ) -> None:
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
        portal_request = _portal_request_value(log_entry.channel)

        if is_rejected:
            self._add_row(
                enquiry_type=_map_enquiry_type(is_rejected=True),
                client_name=str(full_name),
                description=log_entry.rejection_reason or "Validation failed",
                resolution_progress="Pending correction from client",
                rejection_risk="High",
                today=today,
                log_entry=log_entry,
                portal_request=portal_request,
            )

        for flag in triggered:
            self._add_row(
                enquiry_type=_map_enquiry_type(False, flag.label, flag.reason),
                client_name=str(full_name),
                description=f"{flag.label}: {flag.reason}" if flag.reason else flag.label,
                resolution_progress="Pending follow-up with client",
                rejection_risk=flag.rejection_risk,
                today=today,
                log_entry=log_entry,
                portal_request=portal_request,
            )

    def _add_row(
        self, *, enquiry_type: str, client_name: str, description: str,
        resolution_progress: str, rejection_risk: str, today: str,
        log_entry: ProcessingLogEntry, portal_request: str,
    ) -> None:
        self._counter += 1
        row = {
            "No.": self._counter,
            "Date": today,
            "Type of Enquiry": enquiry_type,
            "Client Name": client_name,
            "Query Description": description,
            "Logged Via": log_entry.channel,
            "Registered by": log_entry.captured_by,
            "Assigned to": "",
            "Checked by": "",
            "Resolution Progress": resolution_progress,
            "Date Submitted to Ops / Resolved": "",
            "Date Captured": log_entry.upload_date,
            "Status": "Open",
            "Investor Portal Request": portal_request,
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

    # -----------------------------------------------------------------
    # Query Log sheet
    # -----------------------------------------------------------------

    def _write_query_log(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Query Log")

        # Row 1: "Operations Use ONLY" banner, merged over the 3 Ops-only
        # columns (Sales Comments to Ops / Ops Resolution / Ops Resolution
        # Date - the last 3 columns), matching BIFM's file exactly.
        last3_start = len(_COLUMNS) - 2  # 1-indexed start of the last 3 columns
        start_letter = _COL_LETTERS[last3_start - 1]
        end_letter = _COL_LETTERS[len(_COLUMNS) - 1]
        ws.merge_cells(f"{start_letter}1:{end_letter}1")
        banner_cell = ws[f"{start_letter}1"]
        banner_cell.value = "Operations Use ONLY"
        banner_cell.font = OPS_BANNER_FONT
        banner_cell.fill = OPS_BANNER_FILL
        banner_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 24

        # Row 2: column headers (blank for the picklist-only columns).
        for idx, (_key, header) in enumerate(_COLUMNS, start=1):
            letter = _COL_LETTERS[idx - 1]
            cell = ws.cell(row=2, column=idx, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[2].height = 26

        # Static picklist source values - written once, not per-row data.
        self._write_picklist(ws, "D", ENQUIRY_TYPE_OPTIONS)
        self._write_picklist(ws, "H", LOGGED_VIA_OPTIONS)
        self._write_picklist(ws, "J", REGISTERED_BY_OPTIONS)
        self._write_picklist(ws, "Q", STATUS_OPTIONS)
        self._write_picklist(ws, "S", PORTAL_REQUEST_OPTIONS)

        # Data rows, starting row 3.
        row_idx = 3
        for row, risk in self._query_rows:
            for key, _header in _COLUMNS:
                if key.startswith("_"):
                    continue  # picklist-only column, no per-row data
                col_idx = _COL_INDEX[key]
                cell = ws.cell(row=row_idx, column=col_idx, value=row.get(key, ""))
                cell.border = THIN_BORDER
                if key in ("Date", "Date Submitted to Ops / Resolved", "Date Captured", "Ops Resolution Date"):
                    cell.number_format = DATE_FORMAT

            risk_l = str(risk).lower()
            fill = FILL_HIGH_RISK if risk_l.startswith("high") else (
                FILL_MEDIUM_RISK if risk_l.startswith("medium") else None
            )
            if fill:
                for key, _header in _COLUMNS:
                    if key.startswith("_"):
                        continue
                    ws.cell(row=row_idx, column=_COL_INDEX[key]).fill = fill
            row_idx += 1

        last_data_row = max(row_idx - 1, 3)
        validation_last_row = last_data_row + VALIDATION_ROW_BUFFER

        self._add_dropdown(ws, "C", f"$D$3:$D${2 + len(ENQUIRY_TYPE_OPTIONS)}", 3, validation_last_row)
        self._add_dropdown(ws, "G", f"$H$3:$H${2 + len(LOGGED_VIA_OPTIONS)}", 3, validation_last_row)
        self._add_dropdown(ws, "I", f"$J$3:$J${2 + len(REGISTERED_BY_OPTIONS)}", 3, validation_last_row)
        self._add_dropdown(ws, "P", f"$Q$3:$Q${2 + len(STATUS_OPTIONS)}", 3, validation_last_row)
        self._add_dropdown(ws, "R", f"$S$3:$S${2 + len(PORTAL_REQUEST_OPTIONS)}", 3, validation_last_row)

        for letter, width in _COLUMN_WIDTHS.items():
            ws.column_dimensions[letter].width = width

        # Matches BIFM's own file: header rows + the first 20 columns
        # (No. through No. of Days Open) stay visible while scrolling
        # into the "Operations Use ONLY" section.
        ws.freeze_panes = "U3"

        if not self._query_rows:
            logger.info("Query Log: no queries, rejections, or missing-documentation flags this run.")

    @staticmethod
    def _write_picklist(ws: Worksheet, column_letter: str, options: list[str]) -> None:
        for i, option in enumerate(options):
            ws[f"{column_letter}{3 + i}"] = option

    @staticmethod
    def _add_dropdown(ws: Worksheet, column_letter: str, source_range: str, first_row: int, last_row: int) -> None:
        dv = DataValidation(type="list", formula1=f"={source_range}", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{column_letter}{first_row}:{column_letter}{last_row}")

    # -----------------------------------------------------------------
    # Recon sheet
    # -----------------------------------------------------------------

    def _write_recon(self, wb: Workbook) -> None:
        """Matches BIFM's own Recon layout: starts one column in (B, not
        A), with instruction-type row labels bold in column B and counts
        in C/D/E."""
        ws = wb.create_sheet("Recon")

        headers = ["Instruction Type", "Received", "Processed", "Pending"]
        for i, header in enumerate(headers):
            col_idx = 2 + i  # starts at column B
            cell = ws.cell(row=2, column=col_idx, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.border = THIN_BORDER

        row_idx = 3
        for instr_type in RECON_INSTRUCTION_TYPES:
            counts = self._recon_counts[instr_type]
            label_cell = ws.cell(row=row_idx, column=2, value=instr_type)
            label_cell.font = Font(bold=True)
            label_cell.border = THIN_BORDER
            for i, col_key in enumerate(("Received", "Processed", "Pending")):
                cell = ws.cell(row=row_idx, column=3 + i, value=counts[col_key])
                cell.border = THIN_BORDER
            row_idx += 1

        total_cell = ws.cell(row=row_idx, column=2, value="TOTAL")
        total_cell.font = Font(bold=True)
        for i, col_key in enumerate(("Received", "Processed", "Pending")):
            total = sum(self._recon_counts[t][col_key] for t in RECON_INSTRUCTION_TYPES)
            cell = ws.cell(row=row_idx, column=3 + i, value=total)
            cell.font = Font(bold=True)

        ws.column_dimensions["B"].width = 26
        for letter in ("C", "D", "E"):
            ws.column_dimensions[letter].width = 14