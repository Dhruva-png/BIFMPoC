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
    vision_model: str = os.environ.get("OLLAMA_VISION_MODEL", "llava")
    request_timeout_seconds: int = 180
    max_retries: int = 2


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
    pdf_render_dpi: int = 300  # bumped from 200 - handwriting needs more detail than typed text
    ocr_enhance_images: bool = True  # grayscale + contrast + sharpen pass before sending to the vision model
    ocr_min_long_edge_px: int = 2000  # upscale pages smaller than this so small handwriting stays legible
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    excel_report_name: str = "BIFM_UT_Processing_Report.xlsx"


settings = AppSettings()
settings.paths.ensure_exist()
