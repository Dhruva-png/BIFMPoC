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
class OllamaSettings:
    host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    text_model: str = os.environ.get("OLLAMA_TEXT_MODEL", "llama3.2")
    vision_model: str = os.environ.get("OLLAMA_VISION_MODEL", "qwen3-vl:8b-instruct")
    request_timeout_seconds: int = 180
    max_retries: int = 2
    # Keeps the model resident in memory between calls instead of Ollama
    # unloading it after its default 5-minute idle window - avoids a costly
    # reload on every document in a batch.
    keep_alive: str = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
    # Caps how many tokens the model can generate per call. The JSON payloads
    # this app needs are small; a lower cap stops the model "running on"
    # and directly cuts wall-clock time per call.
    num_predict: int = int(os.environ.get("OLLAMA_NUM_PREDICT", "600"))


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
