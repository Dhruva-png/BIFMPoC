"""
Loads JSON-based configuration: form types, field definitions, validation rules.
Keeping these as data (not code) means new fields/rules/forms can be added without
touching business logic - matching the "configuration driven design" requirement.
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
    return _load_json("field_definitions.json")


@lru_cache(maxsize=1)
def load_validation_rules() -> dict[str, Any]:
    return _load_json("validation_rules.json")


def clear_config_cache() -> None:
    """Useful in tests, or if config files are hot-reloaded."""
    load_form_types.cache_clear()
    load_field_definitions.cache_clear()
    load_validation_rules.cache_clear()
