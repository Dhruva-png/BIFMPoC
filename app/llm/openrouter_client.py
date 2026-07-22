"""
OpenRouter backend - the LLM backend this app runs on.

Replaces the earlier Groq/Gemini backends: Groq's account had zero
vision-capable models available (every model that used to work returned
404), and the configured Gemini key was invalid. OpenRouter is a single
API that proxies to many providers' models, including several genuinely
free (no credit card) vision-capable ones, so it isn't tied to any one
provider's account-specific breakage the way the old setup was.

Setup (one-time):
  1. Go to https://openrouter.ai/keys (any account, no card needed for
     the free-tier models this app defaults to).
  2. Create an API key.
  3. Set OPENROUTER_API_KEY as an environment variable (or in .env - see
     .env.example) before launching:
       macOS/Linux:  export OPENROUTER_API_KEY="your-key-here"
       Windows:      set OPENROUTER_API_KEY=your-key-here

Default model (OPENROUTER_VISION_MODEL / OPENROUTER_TEXT_MODEL) is
google/gemma-4-31b-it:free - chosen over other free vision options after
checking OpenRouter's own uptime stats: it's served by Google AI Studio
at ~99.5% uptime, versus ~77% for the free NVIDIA OCR-specialist model
that was otherwise a closer thematic fit. A flaky demo is worse than a
slightly-less-specialized model that's actually up when a colleague is
watching.

Free-tier rate limits (per OpenRouter's docs, platform-enforced
regardless of which free model you use): 20 requests/minute, and 50
requests/day until you've purchased at least $10 of credit (never spent
on the free models themselves), after which it's 1000/day. _RpmLimiter
below self-throttles under the per-minute cap so concurrent workers
queue instead of tripping 429s; the per-day cap isn't something this
process can self-throttle around, so a 429 that keeps recurring near the
end of a large batch likely means the daily cap was hit - see
OpenRouterError's message.

Note (worth knowing, not just for us): OpenRouter's free-tier model
pages carry a disclaimer that prompts and outputs sent to :free models
are logged by the hosting provider to improve their product. Fine for
this POC's demo/synthetic data; swap in a paid OpenRouter model (or your
own key with a provider that offers a zero-retention paid tier) before
ever pointing this at real client documents.

Exposes the same public interface as the old app.llm.groq_client /
gemini_client (ask_text, ask_vision, parse_json_response,
check_connection, LLMResponse) so business logic never needs to know
which backend is running - see app.llm.router.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_KEY_CHECK_URL = "https://openrouter.ai/api/v1/key"


class OpenRouterError(Exception):
    """Raised when the OpenRouter API cannot be reached or returns an error."""


class _RpmLimiter:
    """Self-throttles below OpenRouter's free-model requests-per-minute cap
    (20/min platform-wide) so concurrent workers queue instead of finding
    out via a 429. Simple request-count sliding window - unlike Groq's
    token-bucket limiter, OpenRouter's free-tier cap is per-request, not
    per-token, so there's nothing to estimate."""

    def __init__(self, rpm_limit: int, window_seconds: float = 60.0) -> None:
        self._rpm_limit = max(1, rpm_limit)
        self._window = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and (now - self._events[0]) > self._window:
                    self._events.popleft()
                if len(self._events) < self._rpm_limit:
                    self._events.append(now)
                    return
                wait_for = self._window - (now - self._events[0]) + 0.25
            time.sleep(max(wait_for, 0.25))


_rpm_limiter = _RpmLimiter(settings.openrouter.rpm_limit)


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_seconds: float
    raw: dict


def _require_api_key() -> str:
    if not settings.openrouter.api_key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not set. Get a free key (no card required) "
            "at https://openrouter.ai/keys and set it in your .env file "
            "(see .env.example) or as an environment variable."
        )
    return settings.openrouter.api_key


def _parse_retry_after(resp: "requests.Response") -> float:
    header_val = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset")
    if header_val:
        try:
            return float(header_val)
        except ValueError:
            pass
    return 10.0


def _post(model: str, body: dict) -> dict:
    api_key = _require_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_exc: Optional[Exception] = None
    max_attempts = settings.openrouter.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        _rpm_limiter.acquire()
        try:
            start = time.monotonic()
            resp = requests.post(
                _BASE_URL,
                headers=headers,
                json=body,
                timeout=settings.openrouter.request_timeout_seconds,
            )
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                last_exc = OpenRouterError(
                    "Rate limited (429) - free-tier request cap hit (20/min, "
                    "50/day until $10 credit is purchased, then 1000/day)."
                )
                logger.warning(
                    "OpenRouter rate-limited (attempt %d/%d). Backing off %.1fs. "
                    "If this happens near the end of a large batch, the daily "
                    "free-tier cap may be exhausted - see the module docstring.",
                    attempt, max_attempts, retry_after,
                )
                if attempt < max_attempts:
                    time.sleep(retry_after + 1.0)
                    continue
            resp.raise_for_status()
            elapsed = time.monotonic() - start
            data = resp.json()
            data["_elapsed_seconds"] = elapsed
            logger.debug("OpenRouter response in %.1fs (attempt %d)", elapsed, attempt)
            return data

        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            logger.error("Could not reach OpenRouter API (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(3)

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            logger.error("OpenRouter request timed out after %ds (attempt %d/%d)",
                         settings.openrouter.request_timeout_seconds, attempt, max_attempts)
            if attempt < max_attempts:
                time.sleep(2)

        except requests.exceptions.HTTPError as exc:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            last_exc = exc
            logger.error("OpenRouter API error (attempt %d/%d): %s %s", attempt, max_attempts, exc, detail)
            if resp.status_code in (400, 401, 403):
                break  # not retryable - bad key/request, retrying won't help
            if attempt < max_attempts:
                time.sleep(2)

    raise OpenRouterError(f"OpenRouter request failed: {last_exc}") from last_exc


def _extract_text(data: dict) -> str:
    try:
        choices = data.get("choices", [])
        if not choices:
            raise OpenRouterError(f"OpenRouter returned no choices | raw={data}")
        return choices[0].get("message", {}).get("content", "") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"Unexpected OpenRouter response shape: {exc} | raw={data}") from exc


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """Send a reasoning/classification prompt to the OpenRouter text model."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": settings.openrouter.text_model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": settings.openrouter.num_predict,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.debug("ask_text() -> model=%s prompt_len=%d", settings.openrouter.text_model, len(prompt))
    data = _post(settings.openrouter.text_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.openrouter.text_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True) -> LLMResponse:
    """Send a page image plus an extraction prompt to the OpenRouter vision model."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    body: dict = {
        "model": settings.openrouter.vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            ],
        }],
        # Deterministic decoding for transcription - sampling noise just adds
        # inconsistent character-level reads on handwriting.
        "temperature": 0.0,
        "top_p": 0.1,
        "max_tokens": settings.openrouter.num_predict,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.debug("ask_vision() -> model=%s image=%s", settings.openrouter.vision_model, image_path.name)
    data = _post(settings.openrouter.vision_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.openrouter.vision_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def parse_json_response(response_text: str) -> dict:
    """Same defensive JSON extraction the old Groq/Gemini backends used -
    kept identical so callers behave the same regardless of which model is
    actually serving the request behind OpenRouter."""
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
    """Quick health check - confirms an API key is set and OpenRouter will accept it."""
    if not settings.openrouter.api_key:
        return False
    try:
        resp = requests.get(
            _KEY_CHECK_URL,
            headers={"Authorization": f"Bearer {settings.openrouter.api_key}"},
            timeout=5,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False
