from datetime import datetime

from app.models.schemas import Beneficiary, ExtractionResult, FieldValue, InstructionStatus
from app.services.filer import resolve_destination_dir
from app.validation.prevalidation import evaluate_prevalidation_flags


def _new_business_extraction(**field_overrides) -> ExtractionResult:
    ext = ExtractionResult(source_file="NB_Test.pdf", form_code="APPFORM")
    ext.fields["full_name"] = FieldValue("full_name", "Jane Doe", 92)
    for fid, value in field_overrides.items():
        ext.fields[fid] = FieldValue(fid, value, 90)
    return ext


def test_kyc_flag_clears_when_companion_kyc_present_in_batch():
    ext = _new_business_extraction()
    without_companion = evaluate_prevalidation_flags(ext, "APPFORM", batch_form_codes=[])
    with_companion = evaluate_prevalidation_flags(ext, "APPFORM", batch_form_codes=["KYC"])

    assert any(f.flag_id == "kyc_document_absent" and f.triggered for f in without_companion)
    assert not any(f.flag_id == "kyc_document_absent" and f.triggered for f in with_companion)


def test_beneficiary_flags_blank_vs_bad_sum():
    blank = _new_business_extraction()
    blank_flags = evaluate_prevalidation_flags(blank, "APPFORM", batch_form_codes=["KYC"])
    assert any(f.flag_id == "beneficiary_section_blank" and f.triggered for f in blank_flags)

    mismatched = _new_business_extraction()
    mismatched.beneficiaries = [Beneficiary("Kid A", "Child", 50), Beneficiary("Kid B", "Child", 40)]
    mismatched_flags = evaluate_prevalidation_flags(mismatched, "APPFORM", batch_form_codes=["KYC"])
    assert not any(f.flag_id == "beneficiary_section_blank" and f.triggered for f in mismatched_flags)
    assert any(f.flag_id == "beneficiary_percent_not_100" and f.triggered for f in mismatched_flags)


def test_gsgf_quarterly_reminder_always_fires_for_dis_gsg():
    ext = ExtractionResult(source_file="GSGF_Test.pdf", form_code="DIS_GSG")
    flags = evaluate_prevalidation_flags(ext, "DIS_GSG", batch_form_codes=[])
    assert any(f.flag_id == "gsgf_quarterly_cutoff" and f.triggered for f in flags)
    # Doesn't apply to unrelated form types
    other = evaluate_prevalidation_flags(ext, "DIS", batch_form_codes=[])
    assert not any(f.flag_id == "gsgf_quarterly_cutoff" for f in other)


def test_filer_new_business_flat_queues_then_dated_approved_folder():
    as_of = datetime(2026, 7, 9)
    received = resolve_destination_dir("APPFORM", None, InstructionStatus.CAPTURED.value, as_of)
    approved = resolve_destination_dir("APPFORM", None, InstructionStatus.APPROVED.value, as_of)
    rejected = resolve_destination_dir("APPFORM", None, InstructionStatus.REJECTED.value, as_of)

    assert received.as_posix().endswith("New Business/Received")
    assert approved.as_posix().endswith("New Business/Approved/2026/July/09-Jul-2026")
    assert rejected.as_posix().endswith("New Business/Rejected")


def test_filer_dis_gsg_uses_quarterly_not_daily_folder():
    as_of = datetime(2026, 7, 9)
    path = resolve_destination_dir("DIS_GSG", "Non-Money Market (GSGF)", InstructionStatus.CAPTURED.value, as_of)
    assert "Q3-2026" in path.as_posix()
    assert "30-Sep-2026" in path.as_posix()


def test_filer_debit_and_static_use_send_to_awd_dated_folder():
    as_of = datetime(2026, 7, 9)
    debit_path = resolve_destination_dir("DEBIT", "Money Market", InstructionStatus.CAPTURED.value, as_of)
    static_path = resolve_destination_dir("STATIC", None, InstructionStatus.CAPTURED.value, as_of)
    assert debit_path.as_posix().endswith("Debit Orders/2026/July/Send to AWD/09-Jul-2026")
    assert static_path.as_posix().endswith("Static/2026/July/Send to AWD/09-Jul-2026")


def test_filer_dis_standard_splits_money_market_and_rejected():
    as_of = datetime(2026, 7, 9)
    path = resolve_destination_dir("DIS", "Money Market", InstructionStatus.REJECTED.value, as_of)
    assert path.as_posix().endswith("Disinvestments/2026/July/09-Jul-2026/Money Market/Rejected")
