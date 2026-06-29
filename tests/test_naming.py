import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.filer import build_filename


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
