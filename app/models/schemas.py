"""
Domain models passed between pipeline stages. Plain dataclasses, JSON-serialisable,
so they can flow Module 2 -> Module 3 -> Module 4 -> Module 5 exactly as described
in Section 7.2 of the alignment document.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ValidationStatus(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class ConfidenceBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class FieldValue:
    field_id: str
    value: Any
    confidence: float  # 0-100
    source_page: Optional[int] = None


@dataclass
class Beneficiary:
    name: str
    relationship: str
    split_percent: float


@dataclass
class ClassificationResult:
    form_code: str
    form_name: str
    confidence: float
    raw_model_output: str = ""


@dataclass
class ExtractionResult:
    """Output of Module 2: Field Extractor."""
    source_file: str
    form_code: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    beneficiaries: list[Beneficiary] = field(default_factory=list)
    page_count: int = 0

    def field_value(self, field_id: str) -> Any:
        fv = self.fields.get(field_id)
        return fv.value if fv else None

    def to_flat_dict(self) -> dict[str, Any]:
        flat = {fid: fv.value for fid, fv in self.fields.items()}
        flat["source_file"] = self.source_file
        flat["form_code"] = self.form_code
        return flat


@dataclass
class FieldValidationResult:
    field_id: str
    value: Any
    status: ValidationStatus
    message: str = ""


@dataclass
class ValidationReport:
    """Output of Module 3: Validation Engine."""
    source_file: str
    entity_number: str
    results: list[FieldValidationResult] = field(default_factory=list)

    @property
    def overall_status(self) -> ValidationStatus:
        statuses = {r.status for r in self.results}
        if ValidationStatus.FAIL in statuses:
            return ValidationStatus.FAIL
        if ValidationStatus.WARNING in statuses:
            return ValidationStatus.WARNING
        return ValidationStatus.PASS


@dataclass
class ProcessingLogEntry:
    timestamp: str
    original_filename: str
    new_filename: str
    form_type_detected: str
    classification_confidence: float
    validation_status: str
    destination_path: str
    processor_id: str = "marvel.ai-local-poc"

    @staticmethod
    def now(**kwargs) -> "ProcessingLogEntry":
        return ProcessingLogEntry(timestamp=datetime.now().isoformat(timespec="seconds"), **kwargs)


def to_dict(obj) -> dict:
    return asdict(obj)
