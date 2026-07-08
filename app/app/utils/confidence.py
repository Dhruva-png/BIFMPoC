"""
Shared helpers for OCR/vision-model confidence scores.

A vision model claiming 100% confidence on handwritten text is never
honest - there's always some residual doubt - so every confidence score
that ultimately reaches a human (report cells, the Streamlit review UI)
is capped below that ceiling. A separate, lower threshold marks the point
below which a human should double-check the field before it's relied on.

Both numbers live here, in one place, so extraction, classification, and
the report/UI layers all agree on what "confident" and "needs a recheck"
mean.
"""

from __future__ import annotations

CONFIDENCE_CEILING = 98.0   # never display/report a value above this
RECHECK_THRESHOLD = 90.0    # strictly below this -> recommend a manual recheck


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
