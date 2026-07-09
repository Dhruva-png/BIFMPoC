from datetime import datetime

from app.core.pipeline import MISSING_DOCUMENT_FLAG_IDS
from app.models.schemas import Beneficiary, ExtractionResult, FieldValue, InstructionStatus
from app.services.filer import resolve_destination_dir
from app.validation.prevalidation import evaluate_prevalidation_flags


def _new_business_extraction(**field_overrides) -> ExtractionResult:
    ext = ExtractionResult(source_file="NB_Test.pdf", form_code="APPFORM")
    ext.fields["full_name"] = FieldValue("full_name", "Jane Doe", 92)
    for fid, value in field_overrides.items():
        ext.fields[fid] = FieldValue(fid, value, 90)
    return ext


def _kyc_extraction(**field_overrides) -> ExtractionResult:
    ext = ExtractionResult(source_file="KYC_Test.pdf", form_code="KYC")
    ext.fields["full_name"] = FieldValue("full_name", "Jane Doe", 92)
    ext.fields["id_number"] = FieldValue("id_number", "381234567", 92)
    ext.fields["contact_number"] = FieldValue("contact_number", "71234567", 90)
    ext.fields["email"] = FieldValue("email", "jane@example.com", 90)
    ext.fields["authorized_signatory_name"] = FieldValue("authorized_signatory_name", "Jane Doe", 88)
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


def test_filer_debit_and_static_rejected_also_gets_a_day_folder():
    # Rejected DEBIT/STATIC docs don't belong under "Send to AWD" (never
    # queued for AWD authorization), but used to drop the Day folder
    # entirely when routed to "Rejected" instead - inconsistent with every
    # other status leaf in this tree keeping day-level granularity.
    as_of = datetime(2026, 7, 9)
    debit_path = resolve_destination_dir("DEBIT", "Money Market", InstructionStatus.REJECTED.value, as_of)
    static_path = resolve_destination_dir("STATIC", None, InstructionStatus.REJECTED.value, as_of)
    assert debit_path.as_posix().endswith("Debit Orders/2026/July/Rejected/09-Jul-2026")
    assert static_path.as_posix().endswith("Static/2026/July/Rejected/09-Jul-2026")


def test_filer_dis_standard_splits_money_market_and_rejected():
    as_of = datetime(2026, 7, 9)
    path = resolve_destination_dir("DIS", "Money Market", InstructionStatus.REJECTED.value, as_of)
    # Normalized, space-free token ("MoneyMarket") so DIS and ADD route the
    # same investor's documents into the same bucket folder instead of
    # "MoneyMarket" vs "Money Market" siblings - see filer._fund_bucket.
    assert path.as_posix().endswith("Disinvestments/2026/July/09-Jul-2026/MoneyMarket/Rejected")


def test_filer_kyc_gets_a_dated_day_folder_like_every_other_form_type():
    # KYC used to stop at Year/Month with no Day folder - the one form type
    # inconsistent with every other's day-level granularity above.
    as_of = datetime(2026, 7, 9)
    path = resolve_destination_dir("KYC", None, InstructionStatus.CAPTURED.value, as_of)
    assert path.as_posix().endswith("KYC/2026/July/09-Jul-2026")


def test_filer_kyc_segregates_rejected_documents_too():
    as_of = datetime(2026, 7, 9)
    path = resolve_destination_dir("KYC", None, InstructionStatus.REJECTED.value, as_of)
    assert path.as_posix().endswith("KYC/2026/July/09-Jul-2026/Rejected")


def test_kyc_flag_triggers_when_contact_number_missing():
    ext = _kyc_extraction(contact_number="")
    flags = evaluate_prevalidation_flags(ext, "KYC", batch_form_codes=[])
    hit = next(f for f in flags if f.flag_id == "kyc_contact_or_signature_missing")
    assert hit.triggered
    assert "contact_number" in hit.reason


def test_kyc_flag_triggers_when_email_missing():
    ext = _kyc_extraction(email="")
    flags = evaluate_prevalidation_flags(ext, "KYC", batch_form_codes=[])
    hit = next(f for f in flags if f.flag_id == "kyc_contact_or_signature_missing")
    assert hit.triggered
    assert "email" in hit.reason


def test_kyc_flag_triggers_when_signature_missing():
    ext = _kyc_extraction(authorized_signatory_name="")
    flags = evaluate_prevalidation_flags(ext, "KYC", batch_form_codes=[])
    hit = next(f for f in flags if f.flag_id == "kyc_contact_or_signature_missing")
    assert hit.triggered
    assert "authorized_signatory_name" in hit.reason


def test_kyc_flag_clears_when_contact_and_signature_all_present():
    ext = _kyc_extraction()
    flags = evaluate_prevalidation_flags(ext, "KYC", batch_form_codes=[])
    hit = next(f for f in flags if f.flag_id == "kyc_contact_or_signature_missing")
    assert not hit.triggered


def test_kyc_flag_does_not_apply_to_other_form_types():
    ext = _new_business_extraction()  # no contact_number/email/signature captured
    flags = evaluate_prevalidation_flags(ext, "APPFORM", batch_form_codes=[])
    assert not any(f.flag_id == "kyc_contact_or_signature_missing" for f in flags)


def test_kyc_contact_or_signature_flag_routes_batch_to_missing_folder():
    # This is the actual bug report: a KYC document that's PRESENT but
    # missing contact info or a signature used to only show up as a row on
    # the Pre-Validation Flags sheet - it never routed the batch into the
    # "Missing" folder the way an entirely absent KYC document does.
    assert "kyc_contact_or_signature_missing" in MISSING_DOCUMENT_FLAG_IDS
