"""
Thin wrapper around the local Ollama REST API (http://localhost:11434).

Design intent:
- ALL LLM calls in the application go through this module - one place to retry,
  log, and account for cost/latency.
- Two entry points only: ask_text() for llama3.2 reasoning, and ask_vision()
  for llava image understanding. Business logic never talks to requests.post()
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
    for attempt in range(1, settings.ollama.max_retries + 2):
        try:
            start = time.monotonic()
            resp = requests.post(url, json=payload, timeout=settings.ollama.request_timeout_seconds)
            resp.raise_for_status()
            elapsed = time.monotonic() - start
            data = resp.json()
            data["_elapsed_seconds"] = elapsed
            return data
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            logger.error(
                "Could not reach Ollama at %s (attempt %d/%d). Is `ollama serve` running?",
                settings.ollama.host, attempt, settings.ollama.max_retries + 1,
            )
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            logger.warning("Ollama request failed (attempt %d): %s", attempt, exc)
        time.sleep(1.5 * attempt)
    raise OllamaError(f"Ollama request failed after retries: {last_exc}") from last_exc


def ask_text(prompt: str, system: str | None = None, json_mode: bool = False) -> LLMResponse:
    """
    Send a reasoning/classification prompt to llama3.2.
    Use json_mode=True when you need a strictly parseable JSON reply.
    """
    payload = {
        "model": settings.ollama.text_model,
        "prompt": prompt,
        "stream": False,
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
    Send a page image plus an extraction prompt to llava.
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
        "options": {
            # Deterministic, lowest-temperature decoding: for transcription
            # tasks (as opposed to creative generation) sampling noise just
            # produces inconsistent character-level reads of handwriting.
            "temperature": 0.0,
            "top_p": 0.1,
            "num_predict": 1024,
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
    """
    text = response_text.strip()
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
