"""
Shared helpers for OCR/vision-model confidence scores.

A vision model claiming 100% confidence on handwritten text is never
honest - there's always some residual doubt - so every confidence score
that ultimately reaches a human (report cells, the Streamlit review UI)
is capped below that ceiling. A separate, lower threshold marks the point
below which a human should double-check the field before it's relied on.

Both numbers live here, in one place, so extraction, classification, and
the report/UI layers all agree on what "confident" and "needs a recheck"
mean. The domain-constraint calibration constants below live here for the
same reason - app.utils.field_repair decides WHETHER a value satisfies its
known domain, and app.utils.field_validators needs to agree on what that
verdict is worth.
"""

from __future__ import annotations

CONFIDENCE_CEILING = 98.0   # never display/report a value above this
RECHECK_THRESHOLD = 90.0    # strictly below this -> recommend a manual recheck

# --- Domain-constraint calibration (app.utils.field_repair) ---
# A value checked against something we know for certain (an Omang is 9
# digits; occupation is one of 6 listed options; the fund is one of 6 real
# BIFM funds) is far stronger evidence than the model's opinion of its own
# read. These push the *reported* confidence toward what was actually
# verified against that domain.
DOMAIN_REPAIRED_BONUS = 3.0      # was wrong but unambiguously fixable -> now provably legal
DOMAIN_VIOLATION_PENALTY = 30.0  # violates a known constraint and couldn't be safely fixed


def cap_confidence(raw: float) -> float:
    """Clamps a raw 0-100 model confidence score into [0, CONFIDENCE_CEILING]."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(value, CONFIDENCE_CEILING))


def needs_recheck(confidence: float) -> bool:
    """True if this confidence score is low enough to warrant a manual recheck."""
    return confidence < RECHECK_THRESHOLD