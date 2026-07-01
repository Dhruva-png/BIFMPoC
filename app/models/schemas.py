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


class InstructionStatus(str, Enum):
    """
    AWD-style workflow state (Section: 'Metadata Fields for Filing' in the
    requirements doc). This is deliberately separate from ValidationStatus:
    ValidationStatus is an OCR/data-quality signal, InstructionStatus is the
    human workflow state. An OCR PASS does not mean AWD has approved it -
    that decision belongs to the senior authorizer in the dual-capture step,
    which is outside this POC's scope. This POC sets:
      CAPTURED  - OCR extraction + validation completed with no FAIL
      REJECTED  - validation FAILed (mandatory field missing / bad format)
    APPROVED remains for the human authorizer to set downstream in AWD;
    the column is included in the Excel output so Ops can populate it there.
    """
    SUBMITTED = "Submitted"
    CAPTURED = "Captured"
    APPROVED = "Approved"
    REJECTED = "Rejected"


class Channel(str, Enum):
    EMAIL = "Email"
    WALK_IN = "Walk-in"
    UNKNOWN = "Unknown"


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
    # --- Metadata Fields for Filing (requirements doc table) ---
    upload_date: str = ""                 # system timestamp at intake, not processing time
    channel: str = Channel.UNKNOWN.value  # Email / Walk-in, tagged at upload
    instruction_status: str = InstructionStatus.SUBMITTED.value
    rejection_reason: str = ""            # populated automatically on FAIL, editable by Ops
    captured_by: str = "marvel.ai-local-poc"  # User ID; this POC auto-captures via OCR
    authorized_by: str = ""               # left blank - set by the human authorizer in AWD

    @staticmethod
    def now(**kwargs) -> "ProcessingLogEntry":
        return ProcessingLogEntry(timestamp=datetime.now().isoformat(timespec="seconds"), **kwargs)


def to_dict(obj) -> dict:
    return asdict(obj)
