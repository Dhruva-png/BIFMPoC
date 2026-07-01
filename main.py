"""
Entry point for the BIFM UT POC application.

Usage:
    python main.py                 # launches the desktop GUI
    python main.py --headless      # runs a single batch over the intake folder, no GUI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger("main")


def run_headless(intake: str | None, channel: str = "Unknown") -> int:
    from app.llm.router import check_connection
    from app.core.pipeline import run_batch

    if not check_connection():
        from config.settings import settings
        if settings.llm_provider == "ollama":
            print("ERROR: Could not reach Ollama at the configured host. Start it with `ollama serve` "
                  "and ensure llama3.2 and qwen3-vl:8b-instruct are pulled "
                  "(`ollama pull llama3.2 && ollama pull qwen3-vl:8b-instruct`).")
        elif settings.llm_provider == "gemini":
            print("ERROR: Could not reach Gemini (missing/invalid GEMINI_API_KEY, or quota is 0). "
                  "Get a free key at https://aistudio.google.com/apikey and set it as an "
                  "environment variable, or set LLM_PROVIDER=groq or LLM_PROVIDER=ollama instead.")
        else:
            print("ERROR: Could not reach Groq (missing or invalid GROQ_API_KEY). "
                  "Get a free key (no card) at https://console.groq.com/keys and set it as an "
                  "environment variable, or set LLM_PROVIDER=ollama to run fully local.")
        return 1

    intake_dir = Path(intake) if intake else None
    outcomes, report_path = run_batch(intake_dir, progress_cb=print, channel=channel)

    print("\n--- Batch Summary ---")
    for o in outcomes:
        print(f"{o.filename:40s} form={o.form_code:10s} confidence={o.classification_confidence:5.1f}  status={o.validation_status}")
    print(f"\nReport: {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BIFM Unit Trusts - local document processing POC")
    parser.add_argument("--headless", action="store_true", help="Run a batch without launching the GUI")
    parser.add_argument("--intake", type=str, default=None, help="Override the intake folder for this run")
    parser.add_argument("--channel", type=str, default="Unknown", choices=["Email", "Walk-in", "Unknown"],
                         help="Submission channel tag applied to every document in this run")
    args = parser.parse_args()

    if args.headless:
        return run_headless(args.intake, args.channel)

    from app.ui.main_window import launch
    launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
