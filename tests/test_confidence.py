import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.confidence import CONFIDENCE_CEILING, RECHECK_THRESHOLD, cap_confidence, needs_recheck
from app.models.schemas import FieldValue
from app.services.extractor import _derive_metadata


def test_ceiling_and_threshold_values():
    # Locks in the specific numbers requested: max 98, recheck below 90.
    assert CONFIDENCE_CEILING == 98.0
    assert RECHECK_THRESHOLD == 90.0


def test_raw_100_is_capped_to_ceiling():
    assert cap_confidence(100) == 98.0


def test_values_under_ceiling_pass_through_unchanged():
    assert cap_confidence(73) == 73.0
    assert cap_confidence(0) == 0.0


def test_negative_or_garbage_confidence_is_clamped_safely():
    assert cap_confidence(-5) == 0.0
    assert cap_confidence("not a number") == 0.0


def test_needs_recheck_threshold_is_exclusive_at_90():
    assert needs_recheck(89.9) is True
    assert needs_recheck(90.0) is False
    assert needs_recheck(95.0) is False
    assert needs_recheck(0.0) is True


def test_derived_metadata_fields_no_longer_show_100_percent():
    # _derive_metadata's own default confidence (for system-computed fields
    # like form_type, fund_category, etc.) must also respect the ceiling.
    derived = _derive_metadata("ADD", {"fund_name": FieldValue(field_id="fund_name", value="BIFM Pula Money Market Fund", confidence=95.0)})
    for fv in derived.values():
        assert fv.confidence <= CONFIDENCE_CEILING


if __name__ == "__main__":
    test_ceiling_and_threshold_values()
    test_raw_100_is_capped_to_ceiling()
    test_values_under_ceiling_pass_through_unchanged()
    test_negative_or_garbage_confidence_is_clamped_safely()
    test_needs_recheck_threshold_is_exclusive_at_90()
    test_derived_metadata_fields_no_longer_show_100_percent()
    print("All confidence tests passed.")
