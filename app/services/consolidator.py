from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.models.schemas import ExtractionResult, FieldValue
from app.utils.config_loader import get_field_type, get_fields_for_form
from app.utils.confidence import RECHECK_THRESHOLD
from app.utils.logger import get_logger

logger = get_logger(__name__)
SHAREABLE_FIELDS: tuple[str, ...] = (
    "entity_number", "entity_name", "full_name", "title",
    "id_number", "id_type", "id_expiry_date", "date_of_birth", "gender",
    "citizenship", "email", "contact_number",
    "residential_address", "postal_address", "occupation",
    "bank_account_name", "bank_name", "account_number",
    "branch_name", "branch_code", "account_type_banking",
    "authorized_signatory_name", "capacity",
)
_BACKFILL_CONFIDENCE_CAP = 85.0


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y"]


def _normalize(field_id: str, value: Any) -> Any:
    if value is None:
        return None

    field_type = get_field_type(field_id)
    text = str(value).strip()
    if text == "":
        return None

    if field_type == "phone":
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

    if field_type == "id_number":
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
    profile: dict[str, FieldValue] = {}

    for field_id in SHAREABLE_FIELDS:
        candidates: list[tuple[ExtractionResult, FieldValue]] = []
        for extraction in extractions:
            fv = extraction.fields.get(field_id)
            if fv is None or _is_blank(fv.value):
                continue
            candidates.append((extraction, fv))

        if not candidates:
            continue

        groups: dict[Any, list[tuple[ExtractionResult, FieldValue]]] = {}
        for extraction, fv in candidates:
            groups.setdefault(_normalize(field_id, fv.value), []).append((extraction, fv))

        def _group_rank(item: tuple[Any, list[tuple[ExtractionResult, FieldValue]]]) -> tuple[int, float]:
            _, members = item
            return (len(members), max(fv.confidence for _, fv in members))

        winning_key, winning_members = max(groups.items(), key=_group_rank)

        group_sizes = sorted((len(m) for m in groups.values()), reverse=True)
        is_genuine_tie = len(group_sizes) > 1 and group_sizes[0] == group_sizes[1]

        # Within the winning group, display the highest-confidence reading
        # (keeps original casing/formatting rather than the normalized form).
        winner_extraction, winner_fv = max(winning_members, key=lambda pair: pair[1].confidence)

        total_docs = len(candidates)
        agreeing_docs = len(winning_members)
        if len(groups) == 1:
            agreement = f"{agreeing_docs}/{total_docs} document(s) agree"
            resolved_confidence = winner_fv.confidence
        elif is_genuine_tie:
            all_sources = ", ".join(sorted(e.source_file for e, _ in candidates))
            agreement = (
                f"NO MAJORITY — {len(groups)}-way tie across {total_docs} documents, no value has "
                f"more support than another (sources: {all_sources}) — best guess shown from "
                f"{winner_extraction.source_file}, needs manual review before relying on this value"
            )
            # Never let an unresolved tie masquerade as a confident answer -
            # this also keeps it out of backfill (see backfill_from_profile).
            resolved_confidence = min(winner_fv.confidence, RECHECK_THRESHOLD - 0.1)
            logger.warning(
                "Field '%s' has NO majority across batch (%s-way tie, %d docs total) - "
                "showing best guess '%s' from %s but NOT backfilling it elsewhere",
                field_id, len(groups), total_docs, winner_fv.value, winner_extraction.source_file,
            )
        else:
            disagreeing_sources = ", ".join(
                sorted(
                    e.source_file for e, _ in candidates
                    if _normalize(field_id, e.fields[field_id].value) != _normalize(field_id, winner_fv.value)
                )
            )
            agreement = (
                f"majority vote: {agreeing_docs}/{total_docs} documents agree on this value "
                f"(differs on: {disagreeing_sources})"
            )
            resolved_confidence = winner_fv.confidence
            logger.warning(
                "Field '%s' disagreed across batch - majority value '%s' from %s used; "
                "outlier(s) on: %s",
                field_id, winner_fv.value, winner_extraction.source_file, disagreeing_sources,
            )

        profile[field_id] = FieldValue(
            field_id=field_id,
            value=winner_fv.value,
            confidence=resolved_confidence,
            source_page=winner_fv.source_page,
            source=winner_extraction.source_file,
            agreement=agreement,
        )

    return profile



def backfill_from_profile(
    extraction: ExtractionResult,
    profile: dict[str, FieldValue],
) -> list[str]:
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
        if source_fv.agreement and source_fv.agreement.startswith("NO MAJORITY"):
            # Don't propagate an unresolved tie onto other documents - a
            # coin-flip guess copied onto a blank field is worse than
            # leaving it blank and flagged for manual entry.
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
