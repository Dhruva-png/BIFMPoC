"""
Central configuration for the BIFM UT POC application.
All paths and tunables live here (or are overridden via environment variables / config.ini),
so nothing is hard-coded inside business logic modules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # project root


@dataclass
class GeminiSettings:
    """
    Google Gemini free tier - the recommended default for laptops that can't
    run an 8B vision model locally. gemini-2.0-flash's free tier (as of this
    writing) is generous enough for POC-scale batches: ~15 requests/minute,
    1,500 requests/day, no cost. Get a key at https://aistudio.google.com/apikey
    (Google account, no card required) and set it as GEMINI_API_KEY.
    """
    api_key: str = os.environ.get("GEMINI_API_KEY", "")
    text_model: str = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
    vision_model: str = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.0-flash")
    request_timeout_seconds: int = int(os.environ.get("GEMINI_REQUEST_TIMEOUT", "60"))
    max_retries: int = 2
    num_predict: int = int(os.environ.get("GEMINI_NUM_PREDICT", "800"))


@dataclass
class OllamaSettings:
    host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    text_model: str = os.environ.get("OLLAMA_TEXT_MODEL", "llama3.2")
    vision_model: str = os.environ.get("OLLAMA_VISION_MODEL", "qwen3-vl:8b-instruct")

    # Raised from 180s — the 8B vision model can take 3-5 min on first call
    # (cold model load) or on CPU-only hardware. 600s (10 min) ensures a slow
    # machine doesn't fail a valid job; fast hardware finishes well under 60s.
    # Override with: OLLAMA_REQUEST_TIMEOUT=300
    request_timeout_seconds: int = int(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "600"))

    # Retry once on transient failures (connection blip, brief overload).
    # Don't retry more — a genuine timeout means the model is genuinely slow;
    # retrying wastes time rather than fixing anything.
    max_retries: int = 1

    # Keep the model hot in VRAM/RAM for the whole batch + a generous buffer.
    # Without this Ollama unloads after 5 min of idle, forcing a full reload
    # on the next document — the single biggest source of per-doc slowness.
    keep_alive: str = os.environ.get("OLLAMA_KEEP_ALIVE", "60m")

    # Caps generated tokens per call. Field-extraction JSON is small (~300
    # tokens); 800 gives headroom for verbose models without letting them
    # "run on" for minutes producing unwanted prose.
    num_predict: int = int(os.environ.get("OLLAMA_NUM_PREDICT", "800"))


@dataclass
class PathSettings:
    intake_dir: Path = BASE_DIR / "intake"
    output_dir: Path = BASE_DIR / "output"
    filed_dir: Path = BASE_DIR / "output" / "filed_documents"
    log_dir: Path = BASE_DIR / "logs"
    config_dir: Path = BASE_DIR / "config"
    temp_dir: Path = BASE_DIR / "output" / "_tmp_pages"

    def ensure_exist(self) -> None:
        for p in (self.intake_dir, self.output_dir, self.filed_dir, self.log_dir, self.temp_dir):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class AppSettings:
    # "gemini" (default - free, fast, no local hardware needed) or "ollama"
    # (fully local/offline, needs a GPU/enough RAM for an 8B vision model).
    # Override with LLM_PROVIDER=ollama to go back to fully local.
    llm_provider: str = os.environ.get("LLM_PROVIDER", "gemini").lower()
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    # 220 DPI is a fast default: llava's vision encoder resizes everything to
    # a fixed ~336x336 internally regardless of what you send it, so pushing
    # DPI/resolution higher just costs you base64-encode + transfer time for
    # zero accuracy gain on that model. If you switch to a model that
    # actually uses higher resolution (e.g. qwen2.5vl, llama3.2-vision),
    # raise this via PDF_RENDER_DPI=300 (or higher) to get the benefit.
    pdf_render_dpi: int = int(os.environ.get("PDF_RENDER_DPI", "220"))
    ocr_enhance_images: bool = os.environ.get("OCR_ENHANCE_IMAGES", "true").lower() != "false"
    ocr_min_long_edge_px: int = int(os.environ.get("OCR_MIN_LONG_EDGE_PX", "1600"))
    # How many documents to process concurrently in a batch. >1 overlaps one
    # document's page rendering/enhancement (CPU) with another's network
    # wait on the Ollama call, which helps even on a single GPU. Raise
    # cautiously - if Ollama queues requests serially (default), pushing this
    # too high just adds contention rather than real parallelism.
    max_workers: int = int(os.environ.get("BIFM_MAX_WORKERS", "2"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    excel_report_name: str = "BIFM_UT_Processing_Report.xlsx"


settings = AppSettings()
settings.paths.ensure_exist()
