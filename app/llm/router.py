"""
Single entry point business logic should import instead of talking to a
specific backend directly. Picks the backend based on settings.llm_provider
(env var LLM_PROVIDER, default "groq"):

  groq    - Free, no-credit-card cloud API (app.llm.groq_client). Default.
            No local hardware requirement, no model download, fast.
            Needs GROQ_API_KEY - see app/llm/groq_client.py docstring for
            the 2-minute setup. Recommended when Gemini's free tier isn't
            available for your account/region.
  gemini  - Google's free-tier cloud API (app.llm.gemini_client). No local
            hardware requirement, no model download, fast (typically 1-3s/
            page) when the free tier is actually available for your
            account. Needs GEMINI_API_KEY - see app/llm/gemini_client.py
            docstring for the 2-minute setup.
  ollama  - Fully local/offline via a locally-running Ollama server
            (app.llm.ollama_client). Needs a machine that can run an 8B
            vision model at reasonable speed (a decent GPU, or patience).

All three backends expose the identical function signatures (ask_text,
ask_vision, parse_json_response, check_connection, LLMResponse), so this
module is a thin dispatch layer - classifier.py, extractor.py, and the UI
entry points import from here and never need to know which backend is
actually running.

Switch providers with zero code changes:
    export LLM_PROVIDER=ollama     # go fully local
    export LLM_PROVIDER=gemini     # Google's free cloud API
    export LLM_PROVIDER=groq       # Groq's free cloud API (default)
"""
from __future__ import annotations

from pathlib import Path

from config.settings import settings

if settings.llm_provider == "ollama":
    from app.llm import ollama_client as _backend
elif settings.llm_provider == "gemini":
    from app.llm import gemini_client as _backend
else:
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
    if settings.llm_provider == "ollama":
        return "Ollama (local)"
    if settings.llm_provider == "gemini":
        return "Gemini (cloud, free tier)"
    return "Marvel AI (cloud, free tier)"
