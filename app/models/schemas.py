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
    # "extracted" (read directly off this document) by default. Set to
    # "backfilled:<other source_file>" when a person-level field was blank
    # on this document and filled in from a sibling document in the same
    # batch - see app.services.consolidator. Purely informational/auditable;
    # never affects validation logic.
    source: str = "extracted"
    # Optional human-readable note on how a *consolidated* (batch-level)
    # value was chosen, e.g. "majority vote: 3/4 documents agree" or
    # "NO MAJORITY - 3-way tie ...". Only set by app.services.consolidator
    # on profile-level FieldValues; None for plain per-document extractions.
    agreement: Optional[str] = None


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

    def own_fields(self) -> dict[str, "FieldValue"]:
        """
        Returns only the fields genuinely read off THIS document, excluding
        any that were backfilled in from a sibling document in the same
        batch (see app.services.consolidator.backfill_from_profile, which
        tags a backfilled FieldValue's source as "backfilled:<source_file>").
        Every view of a single document on its own - Investor Master row,
        the individual per-document workbook, the Streamlit "Document
        Detail" panel - should use this instead of `fields` directly, so a
        value copied in to satisfy validation never displays as if it were
        read straight off this page. The batch-wide "Consolidated Investor
        Profile" view is the one place backfilled/merged data belongs.
        """
        return {fid: fv for fid, fv in self.fields.items() if not fv.source.startswith("backfilled:")}

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
class PreValidationFlag:
    """
    One row of the output described in Section 8 of the understanding doc:
    a document-level completeness/consistency signal that surfaces a likely
    AWD rejection cause BEFORE the instruction reaches the human authoriser.
    Distinct from FieldValidationResult (Module 3), which checks the data
    quality of a single field. A PreValidationFlag can span multiple fields,
    companion documents, or timing rules.
    """
    flag_id: str
    label: str
    triggered: bool
    rejection_risk: str  # High / Medium-High / Medium / Process reminder / Process impact
    reason: str = ""


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
