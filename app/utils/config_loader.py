"""
Loads JSON-based configuration: form types, field definitions, validation rules.
Keeping these as data (not code) means new fields/rules/forms can be added without
touching business logic - matching the "configuration driven design" requirement.

Field definitions are now keyed by form_code (APPFORM, ADD, DEBIT, DIS, DIS_GSG,
STATIC, KYC) to support all 6 BIFM UT form types, not just the application form.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.settings import settings


class ConfigError(Exception):
    """Raised when a required configuration file is missing or malformed."""


def _load_json(filename: str) -> dict[str, Any]:
    path: Path = settings.paths.config_dir / filename
    if not path.exists():
        raise ConfigError(f"Required config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Malformed JSON in {path}: {exc}") from exc


@lru_cache(maxsize=1)
def load_form_types() -> dict[str, Any]:
    return _load_json("form_types.json")


@lru_cache(maxsize=1)
def load_field_definitions() -> dict[str, Any]:
    """
    Returns the full field_definitions.json dict, now structured as:
      { "APPFORM": { "label": ..., "mandatory_fields": [...], "fields": [...] },
        "ADD": { ... }, ... }
    Use get_fields_for_form(form_code) to get fields for a specific form type.
    """
    return _load_json("field_definitions.json")


def get_fields_for_form(form_code: str) -> list[dict]:
    """Returns the field definitions list for a specific form code."""
    definitions = load_field_definitions()
    form_def = definitions.get(form_code, definitions.get("APPFORM", {}))
    return form_def.get("fields", [])


def get_mandatory_fields_for_form(form_code: str) -> list[str]:
    """Returns the mandatory field IDs for a specific form code."""
    definitions = load_field_definitions()
    form_def = definitions.get(form_code, definitions.get("APPFORM", {}))
    return form_def.get("mandatory_fields", [])


@lru_cache(maxsize=1)
def _field_type_index() -> dict[str, str]:
    """field_id -> declared type, indexed once across every form's field list."""
    index: dict[str, str] = {}
    for form_def in load_field_definitions().values():
        for f in form_def.get("fields", []):
            index[f["id"]] = f.get("type", "text")
    return index


def get_field_type(field_id: str) -> str:
    """Returns the declared type for a field_id (e.g. 'phone', 'date',
    'currency', 'id_number', 'email', 'text'), defaulting to 'text' for
    unknown/system-derived field ids."""
    return _field_type_index().get(field_id, "text")


def get_fund_info(fund_name: str) -> dict | None:
    """
    Look up fund metadata by name (case-insensitive substring match).
    Returns dict with keys: name, type (Money Market / Non-Money Market), cutoff_time, lock_in_years.
    """
    if not fund_name:
        return None
    funds = load_form_types().get("funds", [])
    fund_lower = str(fund_name).lower()
    for fund in funds:
        if fund["name"].lower() in fund_lower or fund_lower in fund["name"].lower():
            return fund
    return None


def derive_fund_category(fund_name: str) -> tuple[str, str]:
    """
    Derive fund_category and processing_cutoff from the fund name.
    Returns (fund_category, processing_cutoff) e.g. ("Money Market", "1:00 PM")
    or ("Non-Money Market (GSGF)", "Quarterly - 7th of last month of quarter").
    """
    if not fund_name:
        return ("Unknown", "Unknown")

    fund_lower = str(fund_name).lower()

    # GSGF is a special Non-Money Market with quarterly cut-off
    if "global sustainable growth" in fund_lower or "gsgf" in fund_lower or "gsg" in fund_lower:
        return ("Non-Money Market (GSGF)", "Quarterly - 7th of last month of quarter")

    # Money Market: only the Pula Money Market Fund
    if "money market" in fund_lower or "pula" in fund_lower:
        return ("Money Market", "1:00 PM")

    # All other BIFM funds are Non-Money Market with 3PM cut-off
    if fund_lower and fund_lower != "unknown":
        return ("Non-Money Market", "3:00 PM")

    return ("Unknown", "Unknown")


@lru_cache(maxsize=1)
def load_validation_rules() -> dict[str, Any]:
    return _load_json("validation_rules.json")


@lru_cache(maxsize=1)
def load_prevalidation_flags() -> dict[str, Any]:
    """Section 8 pre-validation flags (rejection-risk signals), separate from
    the field-level validation_rules.json."""
    return _load_json("prevalidation_flags.json")


def clear_config_cache() -> None:
    """Useful in tests, or if config files are hot-reloaded."""
    load_form_types.cache_clear()
    load_field_definitions.cache_clear()
    load_validation_rules.cache_clear()
    load_prevalidation_flags.cache_clear()
    _field_type_index.cache_clear()

def fund_category_priority(fund_category: str | None) -> int:
    """
    Processing-order priority for a resolved fund_category, driven by how
    tight each category's cut-off is — lower number = more time-sensitive:
      1 = Money Market            (1:00 PM cut-off)
      2 = Non-Money Market        (3:00 PM cut-off)
      3 = Non-Money Market (GSGF) (quarterly cut-off)
      99 = Unknown / not resolved (needs manual review, sorts last)
    """
    if not fund_category:
        return 99
    cat = str(fund_category).lower()
    if "gsgf" in cat or "global sustainable" in cat:
        return 3
    if "unknown" in cat:
        return 99
    if "money market" in cat and "non" not in cat:
        return 1
    if "non-money market" in cat or "non money market" in cat:
        return 2
    return 99