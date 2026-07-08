"""
Person-level consolidation across a batch.

Business context: one batch (one intake folder / one Streamlit upload) is
one investor submitting several BIFM UT forms in the same visit - e.g. the
"AMOLEMO" example: STATIC + DIS + DEBIT + ADD all dropped in together.
Investors commonly write their identity/contact/banking details on
whichever form they picked up first and leave those same fields blank on
the others, assuming the branch already has them from the form right next
to it in the stack. Validating each document in total isolation rejects
those as "mandatory field missing" even though the value was captured two
pages over, in the same envelope.

This module:
  1. Builds one consolidated profile from every document's extraction in
     the batch (`build_person_profile`).
  2. Backfills each individual document's blank person-level fields from
     that profile before validation (`backfill_from_profile`) - restricted
     to fields that actually exist on that form type, and never touching
     instruction-specific fields (disinvestment amount, instruction type,
     change type, ...) which must always come from their own document.
  3. Flattens the profile into one row for the "Consolidated Investor
     Profile" report sheet (`profile_to_flat_dict`).
"""
from __future__ import annotations

from app.models.schemas import ExtractionResult, FieldValue
from app.utils.config_loader import get_fields_for_form
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Fields that identify the *person* / their banking relationship, not the
# specific instruction being given on a particular form. Safe to copy
# across every document in the same batch. Deliberately excludes
# instruction-specific fields (disinvestment_amount, instruction_type,
# change_type, lump_sum_deposit_amount, account_closure, ...) - those must
# always be read from their own form, never inferred from a sibling one.
SHAREABLE_FIELDS: tuple[str, ...] = (
    "entity_number", "entity_name", "full_name", "title",
    "id_number", "id_type", "id_expiry_date", "date_of_birth", "gender",
    "citizenship", "email", "contact_number",
    "residential_address", "postal_address", "occupation",
    "fund_name", "fund_number",
    "bank_account_name", "bank_name", "account_number",
    "branch_name", "branch_code", "account_type_banking",
    "authorized_signatory_name", "capacity",
)

# Backfilled values are discounted slightly below their source confidence -
# they were read correctly off *some* page, but not this one, so a human
# reviewer should be able to tell them apart from an on-page extraction.
_BACKFILL_CONFIDENCE_CAP = 85.0


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _normalize_for_vote(value) -> str:
    """Normalizes a value for vote-counting purposes only (case/whitespace-insensitive)."""
    return str(value).strip().lower()


def build_person_profile(extractions: list[ExtractionResult]) -> dict[str, FieldValue]:
    """
    Merges shareable fields across every document's extraction in the
    batch into one profile.

    For each field, collects every non-blank value seen for it across all
    documents in the batch and takes a majority vote: whichever value
    (normalized for case/whitespace) appears most often wins. Ties - and
    fields where every value is unique - fall back to the highest-confidence
    value seen. Among equally-voted/equally-confident candidates, whichever
    document was merged first wins.
    """
    # field_id -> normalized_value -> list of (extraction, fv) that produced it
    candidates: dict[str, dict[str, list[tuple[ExtractionResult, FieldValue]]]] = {}

    for extraction in extractions:
        for field_id in SHAREABLE_FIELDS:
            fv = extraction.fields.get(field_id)
            if fv is None or _is_blank(fv.value):
                continue
            key = _normalize_for_vote(fv.value)
            candidates.setdefault(field_id, {}).setdefault(key, []).append((extraction, fv))

    profile: dict[str, FieldValue] = {}
    for field_id, votes in candidates.items():
        # Best (highest-confidence) occurrence for each distinct value.
        best_per_value = {
            key: max(occurrences, key=lambda pair: pair[1].confidence)
            for key, occurrences in votes.items()
        }
        # Majority vote: most occurrences wins; ties broken by highest confidence,
        # then by earliest occurrence in the batch.
        winning_key = max(
            best_per_value,
            key=lambda key: (
                len(votes[key]),
                best_per_value[key][1].confidence,
                -extractions.index(best_per_value[key][0]),
            ),
        )
        winning_extraction, winning_fv = best_per_value[winning_key]
        profile[field_id] = FieldValue(
            field_id=field_id,
            value=winning_fv.value,
            confidence=winning_fv.confidence,
            source_page=winning_fv.source_page,
            source=winning_extraction.source_file,
        )
    return profile



def backfill_from_profile(
    extraction: ExtractionResult,
    profile: dict[str, FieldValue],
) -> list[str]:
    """
    Fills blank shareable fields on `extraction` from the batch-wide
    profile, in place. Restricted to fields that actually belong on this
    form type (per field_definitions.json) so a field never gets injected
    onto a form that doesn't have a box for it.

    Returns the list of field_ids that were backfilled (for logging/audit).
    """
    valid_field_ids = {f["id"] for f in get_fields_for_form(extraction.form_code)}
    backfilled: list[str] = []

    for field_id in SHAREABLE_FIELDS:
        if field_id not in valid_field_ids:
            continue
        current = extraction.fields.get(field_id)
        if current is not None and not _is_blank(current.value):
            continue
        source_fv = profile.get(field_id)
        if source_fv is None or source_fv.source == extraction.source_file:
            continue

        extraction.fields[field_id] = FieldValue(
            field_id=field_id,
            value=source_fv.value,
            confidence=min(source_fv.confidence, _BACKFILL_CONFIDENCE_CAP),
            source_page=None,
            source=f"backfilled:{source_fv.source}",
        )
        backfilled.append(field_id)

    if backfilled:
        logger.info(
            "Backfilled %d field(s) on %s (%s) from other documents in this batch: %s",
            len(backfilled), extraction.source_file, extraction.form_code, ", ".join(backfilled),
        )
    return backfilled


def profile_to_flat_dict(
    profile: dict[str, FieldValue],
    person_key: str,
    source_files: list[str],
) -> dict:
    """Flattens the consolidated profile into one row for the report."""
    flat = {fid: fv.value for fid, fv in profile.items()}
    flat["person_key"] = person_key
    flat["document_count"] = len(source_files)
    flat["source_documents"] = ", ".join(source_files)
    return flat
