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

import re
from datetime import datetime
from typing import Any

from app.models.schemas import ExtractionResult, FieldValue
from app.utils.config_loader import get_field_type, get_fields_for_form
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
    "bank_account_name", "bank_name", "account_number",
    "branch_name", "branch_code", "account_type_banking",
    "authorized_signatory_name", "capacity",
)
# NOTE: fund_name / fund_number are deliberately NOT shareable. Which fund an
# investor is adding to, disinvesting from, or debiting is instruction-
# specific to that document (e.g. adding to Fund A while disinvesting from
# Fund B in the same visit), never a person-level attribute like their name
# or banking details. Backfilling it across documents previously caused a
# document to display a fund_name copied from a sibling form while its own
# fund_category/processing_cutoff/fund_category_priority (computed once at
# extraction time, from that document's own fund_name) stayed "Unknown" -
# a confusing, silently-wrong combination. If a document's own fund_name
# truly wasn't extracted, it should surface as a genuine validation gap to
# review, not be quietly papered over with an unrelated document's fund.

# Backfilled values are discounted slightly below their source confidence -
# they were read correctly off *some* page, but not this one, so a human
# reviewer should be able to tell them apart from an on-page extraction.
_BACKFILL_CONFIDENCE_CAP = 85.0


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y"]


def _normalize(field_id: str, value: Any) -> Any:
    """
    Normalizes a value for equality-grouping purposes only (the value shown
    in the report is still taken from the original, un-normalized
    FieldValue). This is field-type aware: the previous version only did a
    blanket strip+lowercase on strings, which meant the exact same
    real-world value written slightly differently on two documents -
    "71 234 567" vs "71234567", "2024-03-01" vs "01/03/2024", "P 5,000.00"
    vs "5000" - was treated as two *different* answers. That silently
    fragmented what should have been one clear majority into several
    groups of one, so the "majority vote" frequently ended up picking
    whichever single document had the highest confidence score instead of
    the value most documents actually agreed on - i.e. the reported bug.
    """
    if value is None:
        return None

    field_type = get_field_type(field_id)
    text = str(value).strip()
    if text == "":
        return None

    if field_type in ("phone",):
        digits = re.sub(r"\D", "", text)
        # Drop a Botswana country code or a single leading trunk '0' so
        # the same number written as "71234567", "071234567", "+267 71
        # 234 567" or "267-71-234-567" all collapse to the same 8-digit
        # core for comparison.
        if digits.startswith("267") and len(digits) > 8:
            digits = digits[3:]
        if digits.startswith("0") and len(digits) == 9:
            digits = digits[1:]
        return digits

    if field_type in ("id_number",):
        # Omang/Birth Certificate digits, or an alphanumeric passport
        # number - strip formatting punctuation/spaces, keep case-
        # insensitive comparison for passports.
        return re.sub(r"[\s\-]", "", text).upper()

    if field_type in ("currency", "percentage"):
        stripped = re.sub(r"[^0-9.\-]", "", text)
        try:
            return round(float(stripped), 2)
        except ValueError:
            return text.lower()

    if field_type == "date":
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return text.strip().lower()

    if field_type == "email":
        return text.strip().lower()

    if field_type in ("text", "enum"):
        # Collapse repeated internal whitespace, drop punctuation noise
        # (periods after initials, stray commas/dashes), and casefold -
        # handles "K.  Amolemo", " K Amolemo ", "k amolemo" all being the
        # same name, while still treating genuinely different text as
        # different (no more aggressive merging than that).
        no_punct = re.sub(r"[.,\-]", " ", text)
        collapsed = re.sub(r"\s+", " ", no_punct).strip().casefold()
        return collapsed

    # Fallback for any other/unknown type.
    return text.casefold() if isinstance(value, str) else value


def build_person_profile(extractions: list[ExtractionResult]) -> dict[str, FieldValue]:
    """
    Merges shareable fields across every document's extraction in the
    batch into one profile.

    For each field, groups the non-blank values seen across all documents
    in the batch by their normalized value, and takes the value with the
    most documents agreeing (majority vote) - not simply the single
    highest-confidence reading. This matters because OCR/extraction
    confidence is a per-document signal about how cleanly a page was read;
    it says nothing about whether that page's *content* is correct. A
    cleanly-scanned KYC form with one wrong digit can score a higher
    confidence than three consistent forms that were slightly harder to
    read - the old "keep the highest confidence" logic would have
    surfaced the wrong number. Majority vote uses the batch's redundancy
    (the same person-level fact usually appears on several of their
    forms) to catch that instead.

    Ties (e.g. 2 documents say one value, 2 say another) are broken by
    the highest confidence among the tied groups. Within the winning
    group, the specific value/casing shown is taken from whichever member
    has the highest confidence.
    """
    profile: dict[str, FieldValue] = {}

    for field_id in SHAREABLE_FIELDS:
        # Collect every non-blank (extraction, FieldValue) reading of this
        # field across the batch.
        candidates: list[tuple[ExtractionResult, FieldValue]] = []
        for extraction in extractions:
            fv = extraction.fields.get(field_id)
            if fv is None or _is_blank(fv.value):
                continue
            candidates.append((extraction, fv))

        if not candidates:
            continue

        # Group by normalized value.
        groups: dict[Any, list[tuple[ExtractionResult, FieldValue]]] = {}
        for extraction, fv in candidates:
            groups.setdefault(_normalize(field_id, fv.value), []).append((extraction, fv))

        # Winning group = most documents agreeing; ties broken by the
        # highest confidence seen within that group.
        def _group_rank(item: tuple[Any, list[tuple[ExtractionResult, FieldValue]]]) -> tuple[int, float]:
            _, members = item
            return (len(members), max(fv.confidence for _, fv in members))

        _, winning_members = max(groups.items(), key=_group_rank)

        # Within the winning group, display the highest-confidence reading
        # (keeps original casing/formatting rather than the normalized form).
        winner_extraction, winner_fv = max(winning_members, key=lambda pair: pair[1].confidence)

        total_docs = len(candidates)
        agreeing_docs = len(winning_members)
        if len(groups) == 1:
            agreement = f"{agreeing_docs}/{total_docs} document(s) agree"
        else:
            disagreeing_sources = ", ".join(
                sorted(e.source_file for e, _ in candidates if _normalize(field_id, e.fields[field_id].value) != _normalize(field_id, winner_fv.value))
            )
            agreement = (
                f"majority vote: {agreeing_docs}/{total_docs} documents agree on this value "
                f"(differs on: {disagreeing_sources})"
            )
            logger.warning(
                "Field '%s' disagreed across batch - majority value '%s' from %s used; "
                "outlier(s) on: %s",
                field_id, winner_fv.value, winner_extraction.source_file, disagreeing_sources,
            )

        profile[field_id] = FieldValue(
            field_id=field_id,
            value=winner_fv.value,
            confidence=winner_fv.confidence,
            source_page=winner_fv.source_page,
            source=winner_extraction.source_file,
            agreement=agreement,
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
