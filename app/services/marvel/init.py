"""
MarvelAI integration layer.

This package is intentionally isolated from the OCR extraction pipeline
(app.ocr, app.core, app.llm, app.validation, etc.). It exists purely to
translate data that already exists elsewhere in the application (extraction
results, Excel reports, and — in the future — other sources) into the single
nested-list format MarvelAI accepts.

Nothing in this package modifies, imports internals from, or depends on the
extraction/validation pipeline beyond the public `ExtractionResult` /
`FieldValue` schema types. It is safe to import from the Streamlit app, from
a MarvelAI-facing service, or from a standalone script without any risk of
side effects on existing functionality.
"""
from app.services.marvel.converter import (
    MarvelConverter,
    MarvelRow,
    excel_to_nested_list,
    extraction_result_to_nested_list,
)

__all__ = [
    "MarvelConverter",
    "MarvelRow",
    "excel_to_nested_list",
    "extraction_result_to_nested_list",
]