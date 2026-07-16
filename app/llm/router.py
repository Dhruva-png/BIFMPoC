"""
Single entry point business logic should import instead of talking to the
LLM backend directly.

The app runs on Groq (app.llm.groq_client) - a free, no-credit-card cloud
API with no local hardware requirement and no model download. It needs
GROQ_API_KEY set; see app/llm/groq_client.py's docstring for the 2-minute
setup.

This stays a thin indirection layer (rather than importing groq_client
everywhere) so classifier.py, extractor.py and the UI entry points depend
on a stable interface - ask_text, ask_vision, parse_json_response,
check_connection, LLMResponse - instead of a specific backend module.
"""
from __future__ import annotations

from pathlib import Path

from app.llm import groq_client as _backend

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
    return "Groq (cloud, free tier)"
