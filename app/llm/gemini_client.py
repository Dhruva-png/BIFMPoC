"""
Google Gemini backend - free-tier cloud alternative to running an 8B vision
model locally. Recommended default for laptops without a decent GPU: no local
download, no model warm-up, typically 1-3s per page instead of minutes.

Setup (one-time):
  1. Go to https://aistudio.google.com/apikey (any Google account, no card).
  2. Create an API key.
  3. Set it as an environment variable before launching the app:
       macOS/Linux:  export GEMINI_API_KEY="your-key-here"
       Windows:      set GEMINI_API_KEY=your-key-here
     (or put GEMINI_API_KEY=your-key-here in a .env file if you use one)
  4. Nothing else changes - LLM_PROVIDER defaults to "gemini" already.

Free tier limits (subject to Google changing them, check the AI Studio
dashboard): gemini-2.0-flash is roughly 15 requests/minute and 1,500/day at
no cost, which comfortably covers POC-scale batches (a handful of forms at a
time, a few pages each). If you outgrow it, either request a quota bump or
set LLM_PROVIDER=ollama to fall back to fully local/offline processing.

Same public interface as app.llm.ollama_client (ask_text, ask_vision,
parse_json_response, check_connection, LLMResponse) so business logic never
needs to know which backend is running - see app.llm.router.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


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
            "https://aistudio.google.com/apikey and set it as an environment "
            "variable, or set LLM_PROVIDER=ollama to run fully local instead."
        )
    return settings.gemini.api_key


def _post(model: str, body: dict) -> dict:
    api_key = _require_api_key()
    url = f"{_BASE_URL}/{model}:generateContent"
    last_exc: Optional[Exception] = None
    max_attempts = settings.gemini.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            resp = requests.post(
                url,
                params={"key": api_key},
                json=body,
                timeout=settings.gemini.request_timeout_seconds,
            )
            if resp.status_code == 429:
                # Free-tier rate limit - back off and retry rather than failing
                # the whole batch on a transient quota blip.
                last_exc = GeminiError("Rate limited (429) - free tier quota hit")
                logger.warning(
                    "Gemini rate-limited (attempt %d/%d). Backing off 15s. "
                    "If this happens often, slow down BIFM_MAX_WORKERS.",
                    attempt, max_attempts,
                )
                if attempt < max_attempts:
                    time.sleep(15)
                    continue
            resp.raise_for_status()
            elapsed = time.monotonic() - start
            data = resp.json()
            data["_elapsed_seconds"] = elapsed
            logger.debug("Gemini response in %.1fs (attempt %d)", elapsed, attempt)
            return data

        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            logger.error("Could not reach Gemini API (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(3)

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            logger.error("Gemini request timed out after %ds (attempt %d/%d)",
                         settings.gemini.request_timeout_seconds, attempt, max_attempts)
            if attempt < max_attempts:
                time.sleep(2)

        except requests.exceptions.HTTPError as exc:
            # Surface the actual API error message (bad key, bad model name, etc.)
            # instead of a bare status code - this is the #1 thing people get
            # wrong on first setup.
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            last_exc = exc
            logger.error("Gemini API error (attempt %d/%d): %s %s", attempt, max_attempts, exc, detail)
            if resp.status_code in (400, 401, 403):
                break  # not retryable - bad key/request, retrying won't help
            if attempt < max_attempts:
                time.sleep(2)

    raise GeminiError(f"Gemini request failed: {last_exc}") from last_exc


def _extract_text(data: dict) -> str:
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            block_reason = data.get("promptFeedback", {}).get("blockReason")
            if block_reason:
                raise GeminiError(f"Gemini blocked the request: {block_reason}")
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {exc} | raw={data}") from exc


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """Send a reasoning/classification prompt to the Gemini text model."""
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": settings.gemini.num_predict,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"

    logger.debug("ask_text() -> model=%s prompt_len=%d", settings.gemini.text_model, len(prompt))
    data = _post(settings.gemini.text_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.gemini.text_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True) -> LLMResponse:
    """Send a page image plus an extraction prompt to the Gemini vision model."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    body: dict = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            ],
        }],
        "generationConfig": {
            # Deterministic decoding for transcription, matching the local
            # Ollama backend's settings - sampling noise just adds
            # inconsistent character-level reads on handwriting.
            "temperature": 0.0,
            "topP": 0.1,
            "maxOutputTokens": settings.gemini.num_predict,
        },
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"

    logger.debug("ask_vision() -> model=%s image=%s", settings.gemini.vision_model, image_path.name)
    data = _post(settings.gemini.vision_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.gemini.vision_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def parse_json_response(response_text: str) -> dict:
    """Same defensive JSON extraction as the Ollama backend - kept identical
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
    """Quick health check - confirms an API key is set and Google will accept it."""
    if not settings.gemini.api_key:
        return False
    try:
        resp = requests.get(
            f"{_BASE_URL}/{settings.gemini.text_model}",
            params={"key": settings.gemini.api_key},
            timeout=5,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False
