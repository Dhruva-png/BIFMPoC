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


def _parse_429(resp: "requests.Response") -> tuple[bool, float]:
    """
    Inspect a 429 response body and decide:
      - is_hard_quota: True if any quota metric has limit=0, meaning this key/
        project has NO free-tier allowance at all. Retrying can never succeed
        in that case (Google isn't going to grant more quota mid-backoff), so
        the caller should fail fast instead of burning the whole retry budget.
      - retry_after: seconds Google itself suggests waiting (from the
        RetryInfo field), falling back to 15s if it wasn't provided.
    """
    retry_after = 15.0
    is_hard_quota = False
    try:
        payload = resp.json().get("error", {})
        for detail in payload.get("details", []):
            detail_type = detail.get("@type", "")
            if detail_type.endswith("QuotaFailure"):
                for violation in detail.get("violations", []):
                    if violation.get("quotaValue") in ("0", 0):
                        is_hard_quota = True
            if detail_type.endswith("RetryInfo"):
                delay = detail.get("retryDelay", "")
                if delay.endswith("s"):
                    try:
                        retry_after = float(delay[:-1])
                    except ValueError:
                        pass
        # Some responses only put "limit: 0" in the free-text message rather
        # than structured details - catch that too.
        if "limit: 0" in payload.get("message", ""):
            is_hard_quota = True
    except Exception:  # noqa: BLE001
        pass
    return is_hard_quota, retry_after


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
                is_hard_quota, retry_after = _parse_429(resp)
                if is_hard_quota:
                    # limit: 0 means this project has NO free-tier allowance -
                    # retrying (even with backoff) cannot succeed, so stop
                    # immediately instead of wasting the whole batch's time.
                    raise GeminiError(
                        "Gemini quota is 0 for this API key/project - the free "
                        "tier is unavailable (often due to region restrictions "
                        "or Google disabling no-billing access). This will not "
                        "resolve by retrying. Fix by either: (1) enabling "
                        "billing on the Google Cloud project behind this key at "
                        "https://aistudio.google.com/apikey, or (2) setting "
                        "LLM_PROVIDER=ollama to run fully local instead."
                    )
                last_exc = GeminiError("Rate limited (429) - free tier quota hit")
                logger.warning(
                    "Gemini rate-limited (attempt %d/%d). Backing off %.0fs "
                    "(Google-suggested delay). If this happens often, slow "
                    "down BIFM_MAX_WORKERS.",
                    attempt, max_attempts, retry_after,
                )
                if attempt < max_attempts:
                    time.sleep(retry_after)
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
