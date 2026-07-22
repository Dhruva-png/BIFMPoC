"""
Use Case 2 (BIFM Ops) shared dataclasses.

An OpsDocument is one incoming document (email body, email attachment, or
dropped file) carrying the metadata table from the proposal's "Metadata
Extraction & Management" section. A TransactionGroup is the set of
documents the correlator decided belong to one Withdrawal/Contribution
transaction; an AuditPack is that group evaluated against the configured
pack composition and compiled into the centralized audit repository.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# The metadata fields from the proposal's table, in one place so the
# extractor, repository schema, search, and report all agree.
METADATA_FIELDS = [
    "portfolio_code",       # Primary identifiers for audit retrieval
    "portfolio_name",
    "client_name",          # Identifies the customer
    "transaction_type",     # Withdrawal or Contribution
    "instruction_type",     # Withdrawal Instruction, Deposit Confirmation, ...
    "transaction_date",     # instruction date
    "trade_date",           # trade execution date
    "email_date",           # date the client instruction was received
    "transaction_amount",   # Withdrawal or Contribution amount
    "security_name",        # Investment instrument involved
    "security_code",
    "trade_id",             # Used to correlate transaction documents
    "document_type",        # Letter, Email, PDF, Trade Order, ...
    "source_document",      # Email, Banking Platform, Internal System
    "salesperson",          # DEMO capability - see ops/salesperson.py
]


@dataclass
class OpsDocument:
    source_file: str                      # original filename / message id
    source_path: str                      # where the file currently sits on disk
    doc_type_code: str = "UNRECOGNIZED"   # ops_document_types.json code
    doc_type_name: str = "Unrecognized document"
    classification_confidence: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)   # METADATA_FIELDS -> value
    metadata_confidence: dict[str, float] = field(default_factory=dict)
    review_flags: list[str] = field(default_factory=list)    # human-in-the-loop reasons
    assigned_team: str = ""               # routing outcome (Investment / Operations Team)
    transaction_key: str = ""             # set by the correlator
    filed_path: str = ""                  # Year/Month/Date/Transaction location
    error: str = ""
    # How metadata["salesperson"] was determined - "Extracted" (the document
    # stated one), "Assigned (demo roster)" (fell back to the DEMO roster -
    # see ops/salesperson.py), or "" (neither). Kept outside `metadata` since
    # it describes provenance, not an extracted value in its own right.
    salesperson_source: str = ""

    def meta(self, field_id: str) -> str:
        return str(self.metadata.get(field_id) or "")

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags)


@dataclass
class TransactionGroup:
    transaction_key: str                  # trade id, or composite key
    transaction_type: str                 # Withdrawal / Contribution / Unknown
    portfolio_code: str
    portfolio_name: str
    client_name: str
    transaction_date: str
    transaction_amount: str
    trade_id: str
    salesperson: str = ""                 # DEMO capability - see ops/salesperson.py
    documents: list[OpsDocument] = field(default_factory=list)

    @property
    def doc_type_codes(self) -> set[str]:
        return {d.doc_type_code for d in self.documents}


@dataclass
class PackItem:
    code: str
    name: str
    required: bool
    present: bool
    satisfied_by_code: str = ""           # which doc type actually satisfied it
    note: str = ""


@dataclass
class AuditPack:
    transaction: TransactionGroup
    items: list[PackItem] = field(default_factory=list)
    audit_folder: str = ""                # where the pack was compiled to

    @property
    def missing_required(self) -> list[PackItem]:
        return [i for i in self.items if i.required and not i.present]

    @property
    def is_complete(self) -> bool:
        return bool(self.items) and not self.missing_required

    @property
    def status(self) -> str:
        if not self.items:
            # No pack composition is defined for this transaction type
            # (usually an uncorrelated/unknown transaction) - saying
            # "Complete" here would be a lie of omission.
            return "No pack defined - transaction type unknown"
        return "Complete" if self.is_complete else (
            "Incomplete - missing " + ", ".join(i.name for i in self.missing_required)
        )
