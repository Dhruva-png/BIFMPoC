"""
Google Gemini backend - the LLM backend this app runs on. Talks to
Google directly, own key and own quota. No local download, no model
warm-up.

Replaced OpenRouter (which had briefly replaced Groq as the default -
see git history) after a real batch run against actual documents hit
429s on OpenRouter's free-tier shared pool for its default model, caused
by contention from OTHER OpenRouter users rather than anything on this
account (confirmed live: OpenRouter's own /api/v1/key endpoint showed
usage_daily: 0 at the exact moment requests were failing). Gemini talks
to Google directly with its own key and quota - no shared pool to
contend with - and was already proven reliable in the same testing.

Setup (one-time):
  1. Go to https://aistudio.google.com/apikey (any Google account, no card).
  2. Create an API key.
  3. Set GEMINI_API_KEY as an environment variable (or in .env - see
     .env.example) before launching the app:
       macOS/Linux:  export GEMINI_API_KEY="your-key-here"
       Windows:      set GEMINI_API_KEY=your-key-here

Two-key support: set GEMINI_API_KEY_2 as well (same way) to add a second
key. This app processes documents concurrently (a thread pool - see
ops/pipeline.py and app/core/pipeline.py), so several requests can land
in the same second and trip the free tier's per-key rate limit even on
a normal-sized batch. With two keys configured, _generate() below
round-robins every request across both, and a 429 on one key rotates to
the other immediately rather than sleeping - roughly doubling effective
throughput before either key's quota becomes the bottleneck. One key is
still all that's required; everything above works unchanged.

Default model is gemini-3.1-flash-lite (verified against Google's model
list at ai.google.dev/gemini-api/docs/models before picking it - "Stable"
status, multimodal: text/image/video/audio/PDF input, positioned by
Google as their fast/cheap tier for "lightweight agentic tasks and data
extraction", a good match for this app's per-page vision-extraction call
pattern).

Uses Google's official `google-genai` SDK rather than a plain `requests`
call like the old Groq/OpenRouter backends did (both were OpenAI-
compatible REST, so hand-rolling those was straightforward and
dependency-free). Gemini is different: as of mid-2026 Google is
migrating API keys from "standard" keys (the old AIzaSy... format, a
simple `?key=` query param against the generateContent REST endpoint) to
"auth keys" (bound to a service account, the type AI Studio now issues
by default). A live test against a real auth key found that raw REST
calls to the legacy generateContent endpoint (`?key=` query param,
`x-goog-api-key` header, and `Authorization: Bearer` header were all
tried) uniformly fail with 401 "Expected OAuth 2 access token..." - but
the same key works immediately through `google-genai`'s
`client.models.generate_content()`. Rather than reverse-engineer
whatever internal protocol the SDK actually speaks to make that work,
this module just uses the SDK directly - it's Google's own officially-
supported path and isn't guesswork.

Exposes a small interface (ask_text, ask_vision, parse_json_response,
check_connection, LLMResponse) that app.llm.router re-exports as-is, so
business logic never talks to this module directly.
"""
from __future__ import annotations

import itertools
import json
import threading
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


def _require_api_keys() -> list[str]:
    keys = settings.gemini.api_keys
    if not keys:
        raise GeminiError(
            "No Gemini API key is set. Get a free key at "
            "https://aistudio.google.com/apikey and set GEMINI_API_KEY "
            "(and optionally GEMINI_API_KEY_2 for a second key) in your "
            ".env file (see .env.example) or as environment variables."
        )
    return keys


def _key_label(api_key: str) -> str:
    """Never log a full key - just enough of the tail to tell keys apart
    in the log (e.g. distinguishing key 1's failures from key 2's)."""
    return f"...{api_key[-4:]}" if len(api_key) > 4 else "key"


_client_cache: dict[str, genai.Client] = {}
_round_robin_counter = itertools.count()
_round_robin_lock = threading.Lock()


def _get_client(api_key: str) -> genai.Client:
    client = _client_cache.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _client_cache[api_key] = client
    return client


def _next_key(keys: list[str]) -> str:
    """Round-robins across every configured key. This app analyses
    documents concurrently (a thread pool - see ops/pipeline.py and
    app/core/pipeline.py), so spreading requests across all configured
    keys - rather than always using the first - is what actually lets a
    second key add throughput instead of just sitting idle as a backup."""
    with _round_robin_lock:
        idx = next(_round_robin_counter) % len(keys)
    return keys[idx]


def _generate(model: str, contents, config: dict) -> tuple["genai_types.GenerateContentResponse", float]:
    """Calls generate_content with retry/backoff across every configured
    key. Returns (response, elapsed_seconds)."""
    keys = _require_api_keys()
    last_exc: Optional[Exception] = None
    max_attempts = settings.gemini.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        api_key = _next_key(keys)
        client = _get_client(api_key)
        label = _key_label(api_key)
        try:
            start = time.monotonic()
            response = client.models.generate_content(model=model, contents=contents, config=config)
            elapsed = time.monotonic() - start
            logger.debug("Gemini response in %.1fs (attempt %d, key %s)", elapsed, attempt, label)
            return response, elapsed

        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            detail = getattr(exc, "message", None) or str(exc)
            if code == 429:
                last_exc = GeminiError("Rate limited (429) - free tier quota hit")
                logger.warning(
                    "Gemini rate-limited on key %s (attempt %d/%d): %s. Rotating to the "
                    "next configured key.", label, attempt, max_attempts, detail,
                )
                if attempt < max_attempts:
                    # A single extra key is often enough by itself - only
                    # pause once every key in the pool has been tried this
                    # round and still come back rate-limited, rather than
                    # sleeping after every single attempt regardless.
                    if attempt % len(keys) == 0:
                        time.sleep(15.0)
                    continue
            else:
                # 400/401/403/404 - bad key, bad model name, malformed request.
                # Not retryable; retrying can't fix a request that's wrong.
                last_exc = exc
                logger.error("Gemini API error on key %s (attempt %d/%d): %s", label, attempt, max_attempts, detail)
                break

        except genai_errors.ServerError as exc:
            last_exc = exc
            logger.error("Gemini server error on key %s (attempt %d/%d): %s", label, attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(2)

        except Exception as exc:  # noqa: BLE001 - network-level failures from the SDK's transport
            last_exc = exc
            logger.error("Could not reach Gemini API on key %s (attempt %d/%d): %s", label, attempt, max_attempts, exc)
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
    """Quick health check - confirms at least one configured key is set and
    Google will accept it. Uses a minimal real generate_content call
    (capped at a handful of output tokens) rather than a metadata-only
    endpoint, since that's the one call shape already confirmed to work
    with the newer auth-key format - see this module's docstring.

    Checks every configured key and returns True if ANY of them work -
    with two keys set, one dead or exhausted key shouldn't block the app
    from running when the other is fine; _generate() will simply route
    around the bad one for the rest of the run."""
    keys = settings.gemini.api_keys
    if not keys:
        return False
    any_ok = False
    for api_key in keys:
        try:
            client = _get_client(api_key)
            client.models.generate_content(
                model=settings.gemini.text_model,
                contents="Reply with OK.",
                config={"temperature": 0.0, "max_output_tokens": 5},
            )
            any_ok = True
        except Exception:  # noqa: BLE001
            logger.exception("Gemini connectivity check failed for key %s", _key_label(api_key))
    return any_ok
