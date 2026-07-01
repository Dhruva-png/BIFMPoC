"""
Groq backend - free, no-credit-card-required cloud API. Recommended when
Google's Gemini free tier isn't available for your account/region (Google
has been rolling back no-billing access to gemini-2.0-flash) and your
machine isn't powerful enough to run an 8B local vision model via Ollama.

Setup (one-time):
  1. Go to https://console.groq.com/keys (sign up with email or Google
     account - no card required).
  2. Create an API key.
  3. Set it as an environment variable before launching the app:
       macOS/Linux:  export GROQ_API_KEY="your-key-here"
       Windows:      set GROQ_API_KEY=your-key-here
     (or put GROQ_API_KEY=your-key-here in a .env file if you use one)
  4. Set LLM_PROVIDER=groq (or leave it, since it's the default now).

Free tier limits (subject to Groq changing them, check the console
dashboard): the vision model used here typically allows on the order of
~15-30 requests/minute and several thousand/day at no cost - comfortably
covers POC-scale batches (a handful of forms at a time, a few pages each).
Rate limits apply at the organization level (adding more keys won't raise
them). If you outgrow it, either add billing on the Groq console (still far
cheaper than most alternatives) or set LLM_PROVIDER=ollama to fall back to
fully local/offline processing.

Uses Groq's OpenAI-compatible /chat/completions endpoint directly via
`requests` (no extra SDK dependency needed - same pattern as gemini_client).

Same public interface as app.llm.ollama_client / app.llm.gemini_client
(ask_text, ask_vision, parse_json_response, check_connection, LLMResponse)
so business logic never needs to know which backend is running - see
app.llm.router.
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

_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqError(Exception):
    """Raised when the Groq API cannot be reached or returns an error."""


class _TpmLimiter:
    """
    Thread-safe tokens-per-minute limiter.

    The pipeline runs several documents concurrently (settings.max_workers),
    each of which needs a Groq call. Without this, every worker checks
    "am I under the limit?" independently and several can pass that check
    in the same instant, all fire, and collectively blow through the
    account's real TPM budget - exactly what happened in the DEBIT form
    failure: three overlapping calls pushed usage from 29,321 to a
    requested 32,575 against a 30,000 cap, and the 3rd retry attempt still
    landed inside the same crowded window and failed outright.

    This limiter tracks a sliding 60s window of *estimated* token spend
    and blocks a caller until there's genuinely enough budget left, so
    workers queue up cleanly instead of racing each other into a 429.
    It's a best-effort estimate (exact token counts aren't known until the
    response comes back), which is why settings.groq.tpm_limit is set
    below the account's real cap - that gap absorbs the estimation error.
    """

    def __init__(self, tpm_limit: int, window_seconds: float = 60.0) -> None:
        self._tpm_limit = max(1, tpm_limit)
        self._window = window_seconds
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> int:
        while self._events and (now - self._events[0][0]) > self._window:
            self._events.popleft()
        return sum(tokens for _, tokens in self._events)

    def acquire(self, estimated_tokens: int) -> None:
        estimated_tokens = max(1, estimated_tokens)
        while True:
            with self._lock:
                now = time.monotonic()
                used = self._prune(now)
                if used + estimated_tokens <= self._tpm_limit or not self._events:
                    # Reserve the budget now (not after the response comes
                    # back) so the *next* caller sees this request's spend
                    # immediately, not a full round-trip later.
                    self._events.append((now, estimated_tokens))
                    return
                # Not enough headroom yet - wait until the oldest event ages
                # out of the window, then re-check (another thread may have
                # freed up space too).
                wait_for = self._window - (now - self._events[0][0]) + 0.25
            time.sleep(max(wait_for, 0.25))

    def record_actual(self, reserved_tokens: int, actual_tokens: int) -> None:
        """Corrects the ledger once the real usage is known from the API
        response, so the estimate error doesn't compound over a long batch."""
        if actual_tokens <= 0 or actual_tokens == reserved_tokens:
            return
        with self._lock:
            for idx in range(len(self._events) - 1, -1, -1):
                ts, tokens = self._events[idx]
                if tokens == reserved_tokens:
                    self._events[idx] = (ts, actual_tokens)
                    return


_tpm_limiter = _TpmLimiter(settings.groq.tpm_limit)


def _estimate_tokens(body: dict) -> int:
    """
    Rough pre-flight estimate used only to reserve TPM budget before we know
    the real usage. Text: ~4 chars/token. Images: Groq's vision models tile
    and tokenize images independently of file size, so a flat conservative
    per-image estimate is used rather than trying to derive it from base64
    length (which correlates poorly with token cost).
    """
    total = int(body.get("max_completion_tokens", 0) or 0)
    for message in body.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    total += len(part.get("text", "")) // 4
                elif part.get("type") == "image_url":
                    total += 2000  # conservative flat estimate per image tile
    return total


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_seconds: float
    raw: dict


def _require_api_key() -> str:
    if not settings.groq.api_key:
        raise GroqError(
            "GROQ_API_KEY is not set. Get a free key (no card required) at "
            "https://console.groq.com/keys and set it as an environment "
            "variable, or set LLM_PROVIDER=ollama to run fully local instead."
        )
    return settings.groq.api_key


def _parse_rate_limit(resp: "requests.Response") -> float:
    """
    Groq returns a Retry-After header (seconds) on 429s, and/or a
    human-readable message like "Please try again in 1.234s" in the body.
    Prefer the header; fall back to a sane default.
    """
    header_val = resp.headers.get("Retry-After")
    if header_val:
        try:
            return float(header_val)
        except ValueError:
            pass
    try:
        message = resp.json().get("error", {}).get("message", "")
        if "try again in" in message.lower():
            frag = message.lower().split("try again in", 1)[1].strip()
            num = "".join(ch for ch in frag if (ch.isdigit() or ch == "."))
            if num:
                return float(num)
    except Exception:  # noqa: BLE001
        pass
    return 10.0


def _post(model: str, body: dict) -> dict:
    api_key = _require_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_exc: Optional[Exception] = None
    max_attempts = settings.groq.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        # Proactively wait for TPM budget instead of firing blind and
        # finding out via a 429 - this is what actually stops concurrent
        # workers from stacking up and exceeding the account's real limit.
        reserved = _estimate_tokens(body)
        _tpm_limiter.acquire(reserved)
        try:
            start = time.monotonic()
            resp = requests.post(
                _BASE_URL,
                headers=headers,
                json=body,
                timeout=settings.groq.request_timeout_seconds,
            )
            if resp.status_code == 429:
                # We still reserved `reserved` tokens above even though this
                # call didn't actually consume them server-side - correct
                # the ledger back down so we don't self-throttle harder than
                # necessary on the retry.
                _tpm_limiter.record_actual(reserved, 0)
                retry_after = _parse_rate_limit(resp)
                last_exc = GroqError("Rate limited (429) - free tier request/token limit hit")
                logger.warning(
                    "Groq rate-limited (attempt %d/%d). Backing off %.1fs. "
                    "If this happens often, slow down BIFM_MAX_WORKERS or "
                    "lower GROQ_TPM_LIMIT.",
                    attempt, max_attempts, retry_after,
                )
                if attempt < max_attempts:
                    # +1s safety margin: the server's countdown starts at
                    # the moment it responded, not the moment we wake up.
                    time.sleep(retry_after + 1.0)
                    continue
            resp.raise_for_status()
            elapsed = time.monotonic() - start
            data = resp.json()
            data["_elapsed_seconds"] = elapsed
            actual_tokens = int((data.get("usage") or {}).get("total_tokens", 0) or 0)
            _tpm_limiter.record_actual(reserved, actual_tokens)
            logger.debug("Groq response in %.1fs (attempt %d)", elapsed, attempt)
            return data

        except requests.exceptions.ConnectionError as exc:
            _tpm_limiter.record_actual(reserved, 0)
            last_exc = exc
            logger.error("Could not reach Groq API (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(3)

        except requests.exceptions.Timeout as exc:
            _tpm_limiter.record_actual(reserved, 0)
            last_exc = exc
            logger.error("Groq request timed out after %ds (attempt %d/%d)",
                         settings.groq.request_timeout_seconds, attempt, max_attempts)
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
            _tpm_limiter.record_actual(reserved, 0)
            last_exc = exc
            logger.error("Groq API error (attempt %d/%d): %s %s", attempt, max_attempts, exc, detail)
            if resp.status_code in (400, 401, 403):
                break  # not retryable - bad key/request, retrying won't help
            if attempt < max_attempts:
                time.sleep(2)

    raise GroqError(f"Groq request failed: {last_exc}") from last_exc


def _extract_text(data: dict) -> str:
    try:
        choices = data.get("choices", [])
        if not choices:
            raise GroqError(f"Groq returned no choices | raw={data}")
        return choices[0].get("message", {}).get("content", "") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise GroqError(f"Unexpected Groq response shape: {exc} | raw={data}") from exc


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """Send a reasoning/classification prompt to the Groq text model."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": settings.groq.text_model,
        "messages": messages,
        "temperature": 0.0,
        "max_completion_tokens": settings.groq.num_predict,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.debug("ask_text() -> model=%s prompt_len=%d", settings.groq.text_model, len(prompt))
    data = _post(settings.groq.text_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.groq.text_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True) -> LLMResponse:
    """Send a page image plus an extraction prompt to the Groq vision model."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    body: dict = {
        "model": settings.groq.vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            ],
        }],
        # Deterministic decoding for transcription, matching the other
        # backends' settings - sampling noise just adds inconsistent
        # character-level reads on handwriting.
        "temperature": 0.0,
        "top_p": 0.1,
        "max_completion_tokens": settings.groq.num_predict,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.debug("ask_vision() -> model=%s image=%s", settings.groq.vision_model, image_path.name)
    data = _post(settings.groq.vision_model, body)
    return LLMResponse(
        text=_extract_text(data),
        model=settings.groq.vision_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
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
    """Quick health check - confirms an API key is set and Groq will accept it."""
    if not settings.groq.api_key:
        return False
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {settings.groq.api_key}"},
            timeout=5,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False
