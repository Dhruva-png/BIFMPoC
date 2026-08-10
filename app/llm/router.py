"""
Single entry point business logic should import instead of talking to the
LLM backend directly.

app.llm.gemini_client is the only backend (see its docstring, and
config.settings.GeminiSettings, for why OpenRouter was dropped: its
free-tier shared pool for the default model returned real 429s from
Google AI Studio under contention from other OpenRouter users, live-
tested during a real document batch run, even though our own account
quota was untouched). This module stays a thin pass-through rather than
being inlined at every call site, so classifier.py, extractor.py, the
ops/ package, and the UI entry points never need to import a specific
backend module - if the backend ever changes again, only this file and
gemini_client.py need to.
"""
from __future__ import annotations

from pathlib import Path

from app.llm import gemini_client as _backend

LLMResponse = _backend.LLMResponse


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False):
    return _backend.ask_text(prompt, system=system, json_mode=json_mode)


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True):
    return _backend.ask_vision(prompt, image_path, json_mode=json_mode)


def parse_json_response(response_text: str) -> dict:
    return _backend.parse_json_response(response_text)


def check_connection() -> bool:
    return _backend.check_connection()


def active_provider() -> str:
    """Human-readable label for the UI."""
    return "Gemini (cloud)"


def connection_error_message() -> str:
    """Setup instructions for check_connection() failures - shared by every
    entry point (main.py, ops_main.py, ops_app.py) so the message never
    drifts out of sync with the actual backend."""
    return (
        "Could not reach Gemini on any configured key (missing/invalid "
        "GEMINI_API_KEY, or the project's free-tier quota is 0). Get a "
        "free key at https://aistudio.google.com/apikey and set it in "
        "your .env file (see .env.example) as GEMINI_API_KEY - optionally "
        "add a second as GEMINI_API_KEY_2 for higher throughput."
    )
