"""
Single entry point business logic should import instead of talking to the
LLM backend directly.

app.llm.openrouter_client is the only backend (see its docstring for why
Groq and Gemini were dropped). This module stays a thin pass-through
rather than being inlined at every call site, so classifier.py,
extractor.py, the ops/ package, and the UI entry points never need to
import a specific backend module - if the backend ever changes again,
only this file and openrouter_client.py need to.
"""
from __future__ import annotations

from pathlib import Path

from app.llm import openrouter_client as _backend

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
    return "OpenRouter (cloud, free tier)"


def connection_error_message() -> str:
    """Setup instructions for check_connection() failures - shared by every
    entry point (main.py, ops_main.py, ops_app.py) so the message never
    drifts out of sync with the actual backend."""
    return (
        "Could not reach OpenRouter (missing or invalid OPENROUTER_API_KEY). "
        "Get a free key (no card) at https://openrouter.ai/keys and set it "
        "in your .env file (see .env.example) or as an environment variable."
    )
