"""
Tests for app.validation.engine - these run with zero Ollama dependency,
since the validation engine is pure Python.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import Beneficiary, ExtractionResult, FieldValue, ValidationStatus
from app.validation.engine import validate


def _make_extraction(form_code: str = "APPFORM", **overrides) -> ExtractionResult:
    base = {
        "full_name": "Kabo Mantjue",
        "id_number": "123416789",
        "id_type": "Omang",
        "date_of_birth": "1990-05-10",
        "id_expiry_date": "2030-01-01",
        "email": "kabo@example.com",
        "contact_number": "71234567",
        "citizenship": "Botswana",
        "account_type": "Individual",
        "occupation_type": "Salaried",
        "monthly_income_range": "10000-20000",
        "gender": None,
    }
    base.update(overrides)
    fields = {k: FieldValue(field_id=k, value=v, confidence=95.0) for k, v in base.items() if v is not None}
    return ExtractionResult(source_file="test.pdf", form_code=form_code, fields=fields)


def test_happy_path_passes():
    extraction = _make_extraction()
    extraction.beneficiaries = [Beneficiary(name="Jane Doe", relationship="Spouse", split_percent=100)]
    report = validate(extraction)
    assert report.overall_status == ValidationStatus.PASS


def test_happy_path_with_no_beneficiaries_is_warning_not_fail():
    # Beneficiary capture isn't in the mandatory_fields list, so a form with
    # none listed should WARN for review, not FAIL outright.
    extraction = _make_extraction()
    report = validate(extraction)
    assert report.overall_status == ValidationStatus.WARNING


def test_omang_digit_check_disabled_for_now():
    # id_number_format is disabled in config/validation_rules.json ("enabled":
    # false) at the client's request, so a malformed Omang no longer fails
    # validation on its own right now. Flip "enabled" back to true (or delete
    # it) to restore the exact-9-digit check, at which point this should go
    # back to asserting ValidationStatus.FAIL like it used to.
    extraction = _make_extraction(id_number="12345")
    report = validate(extraction)
    assert not any(r.field_id == "id_number" for r in report.results)
    assert report.overall_status != ValidationStatus.FAIL


def test_gender_derived_from_5th_digit_male():
    # 5th digit = '1' -> Male
    extraction = _make_extraction(id_number="123416789")
    report = validate(extraction)
    gender_result = next(r for r in report.results if r.field_id == "gender")
    assert gender_result.value == "Male"
    assert gender_result.status == ValidationStatus.PASS


def test_gender_derived_from_5th_digit_female():
    extraction = _make_extraction(id_number="123426789")
    report = validate(extraction)
    gender_result = next(r for r in report.results if r.field_id == "gender")
    assert gender_result.value == "Female"


def test_id_number_format_rule_currently_disabled():
    # Same disabled-rule note as above - no id_number finding is produced
    # at all right now, for any id_type, since the rule is switched off.
    extraction = _make_extraction(id_type="Passport", id_number="AB1234567XYZ")
    report = validate(extraction)
    assert not any(r.field_id == "id_number" for r in report.results)


def test_birth_certificate_skips_expiry_validation():
    # id_expiry_future is scoped to KYC only.
    extraction = _make_extraction(form_code="KYC", id_type="Birth Certificate", id_number="987654321", id_expiry_date=None)
    report = validate(extraction)
    expiry_result = next(r for r in report.results if r.field_id == "id_expiry_date")
    assert expiry_result.status == ValidationStatus.PASS
    assert "skip" in expiry_result.message.lower() or "no expiry" in expiry_result.message.lower() or expiry_result.message == "Skipped - Birth Certificates do not expire"


def test_expired_id_fails():
    extraction = _make_extraction(form_code="KYC", id_expiry_date="2020-01-01")
    report = validate(extraction)
    expiry_result = next(r for r in report.results if r.field_id == "id_expiry_date")
    assert expiry_result.status == ValidationStatus.FAIL


def test_id_expiry_not_checked_on_non_kyc_forms():
    # The actual bug report: id_expiry_date must never be checked for
    # (or warned about as "not captured") on any form other than KYC.
    extraction = _make_extraction(form_code="APPFORM", id_expiry_date=None)
    report = validate(extraction)
    assert not any(r.field_id == "id_expiry_date" for r in report.results)


def test_invalid_email_fails():
    extraction = _make_extraction(email="not-an-email")
    report = validate(extraction)
    email_result = next(r for r in report.results if r.field_id == "email")
    assert email_result.status == ValidationStatus.FAIL


def test_minor_account_requires_guardian_fields():
    extraction = _make_extraction(account_type="Acting on Behalf of")
    report = validate(extraction)
    fails = [r for r in report.results if r.field_id in ("minor_full_name", "guardian_name", "guardian_id_number") and r.status == ValidationStatus.FAIL]
    assert len(fails) == 3


def test_minor_account_with_guardian_fields_present_passes_those_checks():
    extraction = _make_extraction(
        account_type="Acting on Behalf of",
        minor_full_name="Tumelo Mantjue",
        guardian_name="Kabo Mantjue",
        guardian_id_number="987654321",
    )
    report = validate(extraction)
    fails = [r for r in report.results if r.field_id in ("minor_full_name", "guardian_name", "guardian_id_number") and r.status == ValidationStatus.FAIL]
    assert len(fails) == 0


def test_preference_defaults_applied_when_blank():
    extraction = _make_extraction()
    report = validate(extraction)
    sms = next(r for r in report.results if r.field_id == "pref_sms")
    marketing = next(r for r in report.results if r.field_id == "pref_marketing")
    channel = next(r for r in report.results if r.field_id == "communication_channel")
    assert sms.value is True
    assert marketing.value is True
    assert channel.value == "Email"


def test_beneficiary_split_must_total_100():
    extraction = _make_extraction()
    extraction.beneficiaries = [
        Beneficiary(name="A", relationship="Spouse", split_percent=50),
        Beneficiary(name="B", relationship="Child", split_percent=40),
    ]
    report = validate(extraction)
    ben_result = next(r for r in report.results if r.field_id == "beneficiaries")
    assert ben_result.status == ValidationStatus.FAIL


def test_beneficiary_split_totaling_100_passes():
    extraction = _make_extraction()
    extraction.beneficiaries = [
        Beneficiary(name="A", relationship="Spouse", split_percent=60),
        Beneficiary(name="B", relationship="Child", split_percent=40),
    ]
    report = validate(extraction)
    ben_result = next(r for r in report.results if r.field_id == "beneficiaries")
    assert ben_result.status == ValidationStatus.PASS


def test_mandatory_field_missing_fails():
    extraction = _make_extraction(email=None)
    report = validate(extraction)
    assert report.overall_status == ValidationStatus.FAIL
