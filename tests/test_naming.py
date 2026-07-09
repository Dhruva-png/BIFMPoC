import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.filer import build_filename, flag_missing_documents
from config.settings import settings


def test_naming_convention_matches_spec():
    name = build_filename("APPFORM", "381346789", "Mantjue", ext="pdf", as_of=datetime(2026, 2, 16))
    assert name == "APPFORM_381346789_MANTJUE_20260216.pdf"


def test_naming_convention_disinvestment_example():
    name = build_filename("DIS", "381234567", "Taunyane", ext="pdf", as_of=datetime(2026, 2, 16))
    assert name == "DIS_381234567_TAUNYANE_20260216.pdf"


def test_naming_strips_invalid_characters():
    name = build_filename("APPFORM", "381346789", "O'Brien Jr.", ext="pdf", as_of=datetime(2026, 1, 1))
    assert " " not in name
    assert "'" not in name
    assert "." not in name.replace(".pdf", "")


def test_rejected_naming_includes_reason():
    name = build_filename(
        "ADD", "381346789", "Alomelo", ext="pdf",
        as_of=datetime(2026, 2, 16), rejection_reason="missing bank details",
    )
    assert name == "ADD - Alomelo - missing bank details.pdf"
    # Rejected files omit entity number / date - only form, name, reason.
    assert "381346789" not in name
    assert "20260216" not in name


def test_missing_document_marker_filename_includes_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.paths, "filed_dir", tmp_path)
    destination = flag_missing_documents(
        person_key="Amolemo",
        missing_labels=["NB_Test.pdf: KYC document absent — no KYC form found in batch"],
        reasons=["KYC document absent"],
        form_code="APPFORM",
        as_of=datetime(2026, 2, 16),
    )
    assert destination is not None
    assert destination.exists()
    assert destination.name == "APPFORM - Amolemo - KYC document absent - 20260216.txt"


def test_missing_document_marker_falls_back_without_form_code(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.paths, "filed_dir", tmp_path)
    destination = flag_missing_documents(
        person_key="Amolemo",
        missing_labels=["some doc: some flag — some reason"],
        as_of=datetime(2026, 2, 16),
    )
    assert destination is not None
    assert destination.name.startswith("Missing - Amolemo - ")
