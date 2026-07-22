"""
Single entry point business logic should import instead of talking to a
specific LLM backend directly.

Picks the backend based on settings.llm_provider (env var LLM_PROVIDER,
default "openrouter"):

  openrouter - Free, no-credit-card cloud API proxying many providers'
               models (app.llm.openrouter_client). Default. Needs
               OPENROUTER_API_KEY - see that module's docstring.
  gemini     - Talks to Google directly (app.llm.gemini_client), its own
               GEMINI_API_KEY and rate limits, not OpenRouter's shared
               free-tier caps. See that module's docstring.

Both backends expose the identical function signatures (ask_text,
ask_vision, parse_json_response, check_connection, LLMResponse), so this
module is a thin dispatch layer - classifier.py, extractor.py, the ops/
package, and the UI entry points import from here and never need to know
which backend is actually running.

Switch providers with zero code changes:
    export LLM_PROVIDER=gemini       # Google's API directly
    export LLM_PROVIDER=openrouter   # OpenRouter's free cloud API (default)
"""
from __future__ import annotations

from pathlib import Path

from config.settings import settings

if settings.llm_provider == "gemini":
    from app.llm import gemini_client as _backend
else:
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
    if settings.llm_provider == "gemini":
        return "Gemini (cloud, direct)"
    return "OpenRouter (cloud, free tier)"


def connection_error_message() -> str:
    """Provider-aware setup instructions for check_connection() failures -
    shared by every entry point (main.py, ops_main.py, ops_app.py) so the
    message never drifts out of sync with whichever provider is active."""
    if settings.llm_provider == "gemini":
        return (
            "Could not reach Gemini (missing/invalid GEMINI_API_KEY, or the "
            "project's free-tier quota is 0). Get a free key at "
            "https://aistudio.google.com/apikey and set it in your .env file "
            "(see .env.example) or as an environment variable, or set "
            "LLM_PROVIDER=openrouter to use OpenRouter instead."
        )
    return (
        "Could not reach OpenRouter (missing or invalid OPENROUTER_API_KEY). "
        "Get a free key (no card) at https://openrouter.ai/keys and set it "
        "in your .env file (see .env.example) or as an environment variable."
    )
