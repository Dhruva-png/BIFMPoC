"""
Small export helpers for turning an ExtractionResult into plain Python
data structures for ad-hoc use outside the report/UI (e.g. copy-pasting
into a notebook, feeding into another script).
"""

from __future__ import annotations

from typing import Optional

from app.models.schemas import ExtractionResult


def fields_as_list_of_lists(extraction: Optional[ExtractionResult]) -> list[list]:
    """
    Converts every field on an ExtractionResult into [['field_id', value], ...]
    - e.g. [['fund_name', 'Bifm Pula Money Market Fund'],
    ['disinvestment_amount', '200.00'], ...]. Includes both
    directly-extracted and system-derived fields (form_type, fund_category,
    instruction_mode, etc.) - i.e. everything on extraction.fields, in
    insertion order. Values are left as their native type (str, number,
    bool) rather than stringified.
    """
    if extraction is None:
        return []
    return [[field_id, fv.value] for field_id, fv in extraction.fields.items()]
