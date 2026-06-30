"""
Thin wrapper around the local Ollama REST API (http://localhost:11434).

Design intent:
- ALL LLM calls in the application go through this module - one place to retry,
  log, and account for cost/latency.
- Two entry points only: ask_text() for llama3.2 reasoning, and ask_vision()
  for vision-model image understanding (default qwen3-vl:8b-instruct).
  Business logic never talks to requests.post()
  directly, so the model/host can change in one place (config/settings.py).
- Every call is logged with elapsed time so it's easy to see how many LLM
  calls a batch run actually used - that's how we keep usage deliberate
  rather than incidental.
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


class OllamaError(Exception):
    """Raised when the local Ollama server cannot be reached or returns an error."""


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_seconds: float
    raw: dict


def _post(payload: dict) -> dict:
    url = f"{settings.ollama.host}/api/generate"
    last_exc: Optional[Exception] = None
    max_attempts = settings.ollama.max_retries + 1  # e.g. max_retries=1 → 2 attempts total

    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            resp = requests.post(url, json=payload, timeout=settings.ollama.request_timeout_seconds)
            resp.raise_for_status()
            elapsed = time.monotonic() - start
            logger.debug("Ollama response in %.1fs (attempt %d)", elapsed, attempt)
            data = resp.json()
            data["_elapsed_seconds"] = elapsed
            return data

        except requests.exceptions.ConnectionError as exc:
            # Ollama not running — retry makes sense (user may be starting it)
            last_exc = exc
            logger.error(
                "Could not reach Ollama at %s (attempt %d/%d). Is `ollama serve` running?",
                settings.ollama.host, attempt, max_attempts,
            )
            if attempt < max_attempts:
                time.sleep(3)  # brief pause then retry connection

        except requests.exceptions.Timeout as exc:
            # Model took longer than request_timeout_seconds.
            # Do NOT retry — another full timeout doubles the wait for no benefit.
            # The timeout itself is already generous (default 600s); if it fires
            # the model is genuinely overloaded or the hardware is too slow.
            last_exc = exc
            logger.error(
                "Ollama timed out after %ds on attempt %d — not retrying. "
                "Increase OLLAMA_REQUEST_TIMEOUT or switch to a smaller model.",
                settings.ollama.request_timeout_seconds, attempt,
            )
            break  # exit retry loop immediately

        except requests.exceptions.RequestException as exc:
            last_exc = exc
            logger.warning("Ollama request failed (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(2)

    raise OllamaError(f"Ollama request failed: {last_exc}") from last_exc


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """
    Send a reasoning/classification prompt to llama3.2.
    Use json_mode=True when you need a strictly parseable JSON reply.
    """
    payload = {
        "model": settings.ollama.text_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": settings.ollama.keep_alive,
        "options": {"num_predict": settings.ollama.num_predict},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"

    logger.debug("ask_text() -> model=%s prompt_len=%d", settings.ollama.text_model, len(prompt))
    data = _post(payload)
    return LLMResponse(
        text=data.get("response", ""),
        model=settings.ollama.text_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def ask_vision(prompt: str, image_path: Path, json_mode: bool = True) -> LLMResponse:
    """
    Send a page image plus an extraction prompt to the configured vision model.
    Used for: form classification (page 1), field extraction, checkbox detection.
    """
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": settings.ollama.vision_model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "think": False,
        "keep_alive": settings.ollama.keep_alive,
        "options": {
            # Deterministic, lowest-temperature decoding: for transcription
            # tasks (as opposed to creative generation) sampling noise just
            # produces inconsistent character-level reads of handwriting.
            "temperature": 0.0,
            "top_p": 0.1,
            # Lower cap than the text model - field-extraction JSON is small,
            # so this stops the model "running on" and directly cuts latency.
            "num_predict": settings.ollama.num_predict,
        },
    }
    if json_mode:
        payload["format"] = "json"

    logger.debug("ask_vision() -> model=%s image=%s", settings.ollama.vision_model, image_path.name)
    data = _post(payload)
    return LLMResponse(
        text=data.get("response", ""),
        model=settings.ollama.vision_model,
        elapsed_seconds=data.get("_elapsed_seconds", 0.0),
        raw=data,
    )


def parse_json_response(response_text: str) -> dict:
    """
    Models occasionally wrap JSON in prose or code fences even when format=json
    is requested. This defensively extracts the first valid JSON object.
    Also strips any stray <think>...</think> block some thinking-capable model
    variants emit inline even when not explicitly asked to "think".
    """
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
    """Quick health check - used by the UI on startup to warn the user early."""
    try:
        resp = requests.get(f"{settings.ollama.host}/api/tags", timeout=5)
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False
