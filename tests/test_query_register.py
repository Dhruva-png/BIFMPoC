from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openpyxl import load_workbook

from app.models.schemas import (
    ExtractionResult,
    FieldValue,
    PreValidationFlag,
    ProcessingLogEntry,
    ValidationReport,
    ValidationStatus,
    FieldValidationResult,
)
from app.services.query_register import QueryRegisterBuilder, recon_type_for_form_code


def _make_extraction(form_code: str, full_name: str = "Jane Doe") -> ExtractionResult:
    return ExtractionResult(
        source_file="test.pdf",
        form_code=form_code,
        fields={"full_name": FieldValue(field_id="full_name", value=full_name, confidence=95.0)},
    )


def _make_log_entry(instruction_status: str, rejection_reason: str = "") -> ProcessingLogEntry:
    return ProcessingLogEntry.now(
        original_filename="test.pdf",
        new_filename="ADD_E1_DOE_20260707.pdf",
        form_type_detected="ADD",
        classification_confidence=95.0,
        validation_status="PASS",
        destination_path="/tmp/out",
        instruction_status=instruction_status,
        rejection_reason=rejection_reason,
        channel="Email",
        upload_date="2026-07-07T09:00:00",
    )


def test_recon_type_mapping():
    assert recon_type_for_form_code("ADD") == "Additional Investments"
    assert recon_type_for_form_code("DIS") == "Withdrawals"
    assert recon_type_for_form_code("DIS_GSG") == "Withdrawals"
    assert recon_type_for_form_code("KYC") is None
    assert recon_type_for_form_code("UNKNOWN_CODE") is None


def test_clean_instruction_not_logged_but_counted():
    qr = QueryRegisterBuilder()
    extraction = _make_extraction("ADD")
    validation = ValidationReport(source_file="test.pdf", entity_number="E1", results=[
        FieldValidationResult(field_id="full_name", value="Jane Doe", status=ValidationStatus.PASS),
    ])
    log_entry = _make_log_entry("Captured")
    qr.add_form(extraction, validation, log_entry, prevalidation_flags=[])

    assert len(qr._query_rows) == 0
    assert qr._recon_counts["Additional Investments"]["Received"] == 1
    assert qr._recon_counts["Additional Investments"]["Processed"] == 1


def test_rejected_instruction_logged():
    qr = QueryRegisterBuilder()
    extraction = _make_extraction("DIS")
    validation = ValidationReport(source_file="test.pdf", entity_number="E1", results=[])
    log_entry = _make_log_entry("Rejected", rejection_reason="wrong banking details")
    qr.add_form(extraction, validation, log_entry, prevalidation_flags=[])

    assert len(qr._query_rows) == 1
    row, risk = qr._query_rows[0]
    assert row["Type of Enquiry"] == "Rejected Instruction"
    assert "wrong banking details" in row["Query Description"]
    assert risk == "High"
    assert qr._recon_counts["Withdrawals"]["Pending"] == 1


def test_triggered_flag_logged_even_when_captured():
    qr = QueryRegisterBuilder()
    extraction = _make_extraction("STATIC")
    validation = ValidationReport(source_file="test.pdf", entity_number="E1", results=[])
    log_entry = _make_log_entry("Captured")
    flag = PreValidationFlag(
        flag_id="kyc_missing", label="KYC companion document missing",
        triggered=True, rejection_risk="Medium", reason="No KYC form found in batch",
    )
    qr.add_form(extraction, validation, log_entry, prevalidation_flags=[flag])

    assert len(qr._query_rows) == 1
    row, risk = qr._query_rows[0]
    assert row["Type of Enquiry"] == "Incomplete / Missing Documentation"
    assert risk == "Medium"


def test_save_produces_two_sheets(tmp_path: Path):
    qr = QueryRegisterBuilder()
    extraction = _make_extraction("ADD")
    validation = ValidationReport(source_file="test.pdf", entity_number="E1", results=[])
    log_entry = _make_log_entry("Rejected", rejection_reason="missing signature")
    qr.add_form(extraction, validation, log_entry, prevalidation_flags=[])

    out_path = qr.save(tmp_path / "query_register.xlsx")
    assert out_path.exists()

    wb = load_workbook(out_path)
    assert wb.sheetnames == ["Query Log", "Recon"]

    query_log = wb["Query Log"]
    assert query_log.cell(row=1, column=1).value == "No."
    assert query_log.cell(row=2, column=3).value == "Rejected Instruction"

    recon = wb["Recon"]
    assert recon.cell(row=1, column=1).value == "Instruction Type"
    # Additional Investments row should show 1 Received / 1 Pending.
    header = [recon.cell(row=1, column=c).value for c in range(1, 5)]
    idx = header.index("Received")
    for r in range(2, recon.max_row):
        if recon.cell(row=r, column=1).value == "Additional Investments":
            assert recon.cell(row=r, column=idx + 1).value == 1


def test_add_form_is_thread_safe_under_concurrent_batches():
    # app.core.pipeline.run_batch shares one QueryRegisterBuilder across
    # multiple people's batches running concurrently - self._counter and
    # self._recon_counts are read-modify-write, so a missing lock would
    # silently lose increments (duplicate "No." values, undercounted
    # recon figures) under real concurrency, not just look wrong on paper.
    qr = QueryRegisterBuilder()
    n_calls = 50

    def make_and_add(i: int) -> None:
        extraction = _make_extraction("ADD", full_name=f"Investor {i}")
        validation = ValidationReport(source_file="test.pdf", entity_number="E1", results=[])
        log_entry = _make_log_entry("Rejected", rejection_reason="missing bank details")
        qr.add_form(extraction, validation, log_entry, prevalidation_flags=[])

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(make_and_add, range(n_calls)))

    assert len(qr._query_rows) == n_calls
    assert qr._counter == n_calls
    # No.s must be unique 1..n_calls - a lost increment would produce a
    # duplicate instead of every value appearing exactly once.
    numbers = sorted(row["No."] for row, _ in qr._query_rows)
    assert numbers == list(range(1, n_calls + 1))
    assert qr._recon_counts["Additional Investments"]["Received"] == n_calls
    assert qr._recon_counts["Additional Investments"]["Pending"] == n_calls
