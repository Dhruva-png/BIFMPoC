"""
Google Gemini backend - alternative cloud LLM backend to OpenRouter (the
default). Talks to Google directly rather than through OpenRouter's
shared pool, so it needs its own GEMINI_API_KEY and isn't subject to
OpenRouter's free-model 20/min-50/day caps. No local download, no model
warm-up.

Setup (one-time):
  1. Go to https://aistudio.google.com/apikey (any Google account, no card).
  2. Create an API key.
  3. Set GEMINI_API_KEY as an environment variable (or in .env - see
     .env.example) and set LLM_PROVIDER=gemini before launching the app:
       macOS/Linux:  export GEMINI_API_KEY="your-key-here"
                     export LLM_PROVIDER=gemini
       Windows:      set GEMINI_API_KEY=your-key-here
                     set LLM_PROVIDER=gemini

Default model is gemini-3.1-flash-lite (verified against Google's model
list at ai.google.dev/gemini-api/docs/models before picking it - "Stable"
status, multimodal: text/image/video/audio/PDF input, positioned by
Google as their fast/cheap tier for "lightweight agentic tasks and data
extraction", a good match for this app's per-page vision-extraction call
pattern).

Uses Google's official `google-genai` SDK rather than a plain `requests`
call like the other backends (Groq/OpenRouter are both OpenAI-compatible
REST, so hand-rolling those was straightforward and dependency-free).
Gemini is different: as of mid-2026 Google is migrating API keys from
"standard" keys (the old AIzaSy... format, a simple `?key=` query param
against the generateContent REST endpoint) to "auth keys" (bound to a
service account, the type AI Studio now issues by default). A live test
against a real auth key found that raw REST calls to the legacy
generateContent endpoint (`?key=` query param, `x-goog-api-key` header,
and `Authorization: Bearer` header were all tried) uniformly fail with
401 "Expected OAuth 2 access token..." - but the same key works
immediately through `google-genai`'s `client.models.generate_content()`.
Rather than reverse-engineer whatever internal protocol the SDK actually
speaks to make that work, this module just uses the SDK directly - it's
Google's own officially-supported path and isn't guesswork.

Exposes the same public interface as app.llm.openrouter_client (ask_text,
ask_vision, parse_json_response, check_connection, LLMResponse) so
business logic never needs to know which backend is running - see
app.llm.router.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)


class GeminiError(Exception):
    """Raised when the Gemini API cannot be reached or returns an error."""


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_seconds: float
    raw: dict


def _require_api_key() -> str:
    if not settings.gemini.api_key:
        raise GeminiError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and set it in your .env file "
            "(see .env.example) or as an environment variable, and set "
            "LLM_PROVIDER=gemini."
        )
    return settings.gemini.api_key


_client_cache: dict[str, genai.Client] = {}


def _get_client() -> genai.Client:
    api_key = _require_api_key()
    client = _client_cache.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _client_cache[api_key] = client
    return client


def _generate(model: str, contents, config: dict) -> tuple["genai_types.GenerateContentResponse", float]:
    """Calls generate_content with retry/backoff. Returns (response, elapsed_seconds)."""
    client = _get_client()
    last_exc: Optional[Exception] = None
    max_attempts = settings.gemini.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            response = client.models.generate_content(model=model, contents=contents, config=config)
            elapsed = time.monotonic() - start
            logger.debug("Gemini response in %.1fs (attempt %d)", elapsed, attempt)
            return response, elapsed

        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            detail = getattr(exc, "message", None) or str(exc)
            if code == 429:
                last_exc = GeminiError("Rate limited (429) - free tier quota hit")
                logger.warning(
                    "Gemini rate-limited (attempt %d/%d): %s. If this happens often, "
                    "slow down BIFM_MAX_WORKERS.", attempt, max_attempts, detail,
                )
                if attempt < max_attempts:
                    time.sleep(15.0)
                    continue
            else:
                # 400/401/403/404 - bad key, bad model name, malformed request.
                # Not retryable; retrying can't fix a request that's wrong.
                last_exc = exc
                logger.error("Gemini API error (attempt %d/%d): %s", attempt, max_attempts, detail)
                break

        except genai_errors.ServerError as exc:
            last_exc = exc
            logger.error("Gemini server error (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(2)

        except Exception as exc:  # noqa: BLE001 - network-level failures from the SDK's transport
            last_exc = exc
            logger.error("Could not reach Gemini API (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(3)

    raise GeminiError(f"Gemini request failed: {last_exc}") from last_exc


def _response_dict(response: "genai_types.GenerateContentResponse") -> dict:
    try:
        return response.model_dump(exclude_none=True)
    except Exception:  # noqa: BLE001 - raw is for debugging only, never load-bearing
        return {}


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """Send a reasoning/classification prompt to the Gemini text model."""
    config: dict = {
        "temperature": 0.0,
        "max_output_tokens": settings.gemini.num_predict,
    }
    if system:
        config["system_instruction"] = system
    if json_mode:
        config["response_mime_type"] = "application/json"

    logger.debug("ask_text() -> model=%s prompt_len=%d", settings.gemini.text_model, len(prompt))
    response, elapsed = _generate(settings.gemini.text_model, prompt, config)
    return LLMResponse(
        text=response.text or "",
        model=settings.gemini.text_model,
        elapsed_seconds=elapsed,
        raw=_response_dict(response),
    )


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True) -> LLMResponse:
    """Send a page image plus an extraction prompt to the Gemini vision model."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    image_part = genai_types.Part.from_bytes(data=image_path.read_bytes(), mime_type=mime_type)

    config: dict = {
        # Deterministic decoding for transcription, matching the other
        # backends' settings - sampling noise just adds inconsistent
        # character-level reads on handwriting.
        "temperature": 0.0,
        "top_p": 0.1,
        "max_output_tokens": settings.gemini.num_predict,
    }
    if json_mode:
        config["response_mime_type"] = "application/json"

    logger.debug("ask_vision() -> model=%s image=%s", settings.gemini.vision_model, image_path.name)
    response, elapsed = _generate(settings.gemini.vision_model, [prompt, image_part], config)
    return LLMResponse(
        text=response.text or "",
        model=settings.gemini.vision_model,
        elapsed_seconds=elapsed,
        raw=_response_dict(response),
    )


def parse_json_response(response_text: str) -> dict:
    """Same defensive JSON extraction as the other backends - kept identical
    on purpose so callers behave the same regardless of provider."""
    text = response_text.strip()
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def check_connection() -> bool:
    """Quick health check - confirms an API key is set and Google will accept
    it. Uses a minimal real generate_content call (capped at a handful of
    output tokens) rather than a metadata-only endpoint, since that's the
    one call shape already confirmed to work with the newer auth-key
    format - see this module's docstring."""
    if not settings.gemini.api_key:
        return False
    try:
        client = _get_client()
        client.models.generate_content(
            model=settings.gemini.text_model,
            contents="Reply with OK.",
            config={"temperature": 0.0, "max_output_tokens": 5},
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Gemini connectivity check failed")
        return False
