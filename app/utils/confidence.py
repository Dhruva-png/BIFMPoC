"""
Shared helpers for OCR/vision-model confidence scores.

A vision model claiming 100% confidence on handwritten text is never
honest - there's always some residual doubt - so every confidence score
that ultimately reaches a human (report cells, the Streamlit review UI)
is capped below that ceiling. A separate, lower threshold marks the point
below which a human should double-check the field before it's relied on.

Both numbers live here, in one place, so extraction, classification, and
the report/UI layers all agree on what "confident" and "needs a recheck"
mean. The multi-pass calibration constants below live here too for the
same reason - app.services.extractor and app.utils.field_validators both
need to agree on what "two reads agreed" or "only one pass saw this
field" are actually worth.
"""

from __future__ import annotations

CONFIDENCE_CEILING = 98.0   # never display/report a value above this
RECHECK_THRESHOLD = 90.0    # strictly below this -> recommend a manual recheck

# --- Multi-pass self-consistency calibration (app.services.extractor) ---
# Two independent reads agreeing is real evidence the value is right;
# disagreeing is real evidence it might not be. These push the *reported*
# confidence toward what was actually verified across passes, rather than
# trusting whatever number the model itself printed for a single read.
AGREEMENT_CONFIDENCE_BONUS = 12.0     # added when pass 1 & 2 agree
DISAGREEMENT_CONFIDENCE_CAP = 55.0    # ceiling applied when they don't, pending tie-break pass
SINGLE_PASS_CONFIDENCE_FACTOR = 0.9   # applied when only one pass extracted a value at all


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