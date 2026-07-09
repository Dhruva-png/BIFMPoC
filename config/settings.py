"""
Central configuration for the BIFM UT POC application.
All paths and tunables live here (or are overridden via environment variables / config.ini),
so nothing is hard-coded inside business logic modules.

API keys and other secrets are loaded from a `.env` file in the project
root (see `.env.example` for every key this app reads) so you only ever
enter them once instead of re-exporting environment variables every
session. `.env` is gitignored — don't commit real keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # project root

try:
    from dotenv import load_dotenv
    # override=False: real shell/CI environment variables still win over
    # .env, so .env is purely a convenience default, never a surprise
    # override of something you deliberately set in your shell.
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    # python-dotenv isn't installed (e.g. minimal CI environment) — fall
    # back to whatever's already in the real environment. `pip install
    # -r requirements.txt` includes python-dotenv, so this only matters
    # if someone's running with a stripped-down install.
    pass


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
class GroqSettings:
    """
    Groq's free, no-credit-card cloud API - recommended fallback when
    Google's Gemini free tier is unavailable for your account/region and
    your machine can't run an 8B local vision model via Ollama at a
    reasonable speed. Get a key at https://console.groq.com/keys (email or
    Google sign-in, no card) and set it as GROQ_API_KEY.
    """
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    text_model: str = os.environ.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
    vision_model: str = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    request_timeout_seconds: int = int(os.environ.get("GROQ_REQUEST_TIMEOUT", "60"))
    # Raised from 2 -> 4. The free tier's 30k TPM budget is tight enough that
    # a batch of concurrent documents can legitimately need more than 3
    # attempts to drain the window - failing a document outright (and losing
    # its fields) is worse than one more short wait.
    max_retries: int = int(os.environ.get("GROQ_MAX_RETRIES", "4"))
    num_predict: int = int(os.environ.get("GROQ_NUM_PREDICT", "800"))
    # Proactive tokens-per-minute budget the app self-throttles to, BELOW
    # Groq's actual account limit (30,000 TPM as of writing - check
    # https://console.groq.com/settings/billing for your org's limit).
    # Kept under the real ceiling so concurrent workers "reserve" budget
    # before sending instead of finding out via a 429 afterwards - the
    # earlier failure was exactly 3 workers' calls landing in the same
    # window and pushing 29,321 -> 32,575 used against a 30,000 limit.
    # Override with GROQ_TPM_LIMIT if your account has a different tier.
    tpm_limit: int = int(os.environ.get("GROQ_TPM_LIMIT", "27000"))


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
class SharePointSettings:
    """
    Microsoft Graph app-only (client credentials) connection details for the
    SharePoint document library the UT team currently uses for the manual
    Submission -> Captured -> Approved/Rejected folder workflow (Section 3).
    All optional — leave unset to keep using local-disk intake/filing only.
    """
    tenant_id: str = os.environ.get("SHAREPOINT_TENANT_ID", "")
    client_id: str = os.environ.get("SHAREPOINT_CLIENT_ID", "")
    client_secret: str = os.environ.get("SHAREPOINT_CLIENT_SECRET", "")
    site_id: str = os.environ.get("SHAREPOINT_SITE_ID", "")
    drive_id: str = os.environ.get("SHAREPOINT_DRIVE_ID", "")
    submission_folder: str = os.environ.get("SHAREPOINT_SUBMISSION_FOLDER", "UT Instructions/Submissions")
    # When true, filed documents are also pushed to the matching folder on
    # real SharePoint (in addition to the local filing simulation).
    write_back_enabled: bool = os.environ.get("SHAREPOINT_WRITE_BACK", "false").lower() == "true"


@dataclass
class ImapSettings:
    """
    IMAP connection details for pulling emailed instruction forms
    (Section 3, Step 1: "A client submits a UT instruction form (PDF) via
    email to the contact center"). All optional — leave IMAP_HOST unset to
    keep the email intake option disabled.

    Works with any IMAP mailbox (Office 365 / Outlook, a hosted BIFM
    mailbox, Gmail-via-IMAP, etc.) — not tied to a specific provider.
    For Gmail specifically, IMAP access needs an "App Password" (if
    2-Step Verification is on) rather than the normal account password,
    since Gmail disables plain password IMAP login by default.

    IMAP_FOLDER is the mailbox/folder to search (default "INBOX").
    IMAP_SEARCH_CRITERIA is a standard IMAP SEARCH string (default
    "UNSEEN" - only unread messages), so already-processed emails aren't
    re-pulled every run. IMAP_PROCESSED_FOLDER, if set and the server
    supports it, moves each processed message there after its PDF
    attachments are downloaded (in addition to marking it \\Seen) so the
    contact center inbox stays clean without deleting anything.

    IMAP_MIN_UNREAD_MINUTES (default 60) holds a message back until it's
    been sitting unread for at least this long, so a client who sends the
    form itself in one email and forgets an attachment in a follow-up a
    minute later still gets picked up as one submission, not processed
    prematurely off the first, incomplete email. This is a MINIMUM age,
    not a window - a still-unread message from days ago is just as
    eligible as one from exactly an hour ago.

    IMAP_ALLOWED_SENDERS restricts intake to a known set of sender
    addresses (comma-separated) - anyone else's email is left unread and
    ignored rather than fed into the pipeline. Blank disables the check.
    """
    host: str = os.environ.get("IMAP_HOST", "")
    port: int = int(os.environ.get("IMAP_PORT", "993"))
    username: str = os.environ.get("IMAP_USERNAME", "")
    password: str = os.environ.get("IMAP_PASSWORD", "")
    use_ssl: bool = os.environ.get("IMAP_USE_SSL", "true").lower() == "true"
    folder: str = os.environ.get("IMAP_FOLDER", "INBOX")
    search_criteria: str = os.environ.get("IMAP_SEARCH_CRITERIA", "UNSEEN")
    processed_folder: str = os.environ.get("IMAP_PROCESSED_FOLDER", "BIFM-Processed")
    min_unread_minutes: int = int(os.environ.get("IMAP_MIN_UNREAD_MINUTES", "60"))
    allowed_senders: str = os.environ.get("IMAP_ALLOWED_SENDERS", "shyam.sp@kgisl.com")


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
    llm_provider: str = os.environ.get("LLM_PROVIDER", "groq").lower()
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    groq: GroqSettings = field(default_factory=GroqSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    sharepoint: SharePointSettings = field(default_factory=SharePointSettings)
    imap: ImapSettings = field(default_factory=ImapSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    pdf_render_dpi: int = int(os.environ.get("PDF_RENDER_DPI", "220"))
    ocr_enhance_images: bool = os.environ.get("OCR_ENHANCE_IMAGES", "true").lower() != "false"
    ocr_min_long_edge_px: int = int(os.environ.get("OCR_MIN_LONG_EDGE_PX", "1600"))
    # Runs every page through 2 independent vision reads (enhanced + raw
    # image) and cross-checks them, firing a focused 3rd tie-break pass
    # only on fields where they disagree - see app.services.extractor's
    # module docstring. Roughly doubles Groq API calls per document
    # (triples only for disputed fields). Turn off with
    # MULTI_PASS_EXTRACTION=false if you need to conserve TPM budget on
    # a large batch, at the cost of the confidence-calibration accuracy
    # improvement.
    multi_pass_extraction: bool = os.environ.get("MULTI_PASS_EXTRACTION", "true").lower() != "false"
    max_workers: int = int(os.environ.get("BIFM_MAX_WORKERS", "2"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    excel_report_name: str = "BIFM_UT_Processing_Report.xlsx"
    query_register_report_name: str = "BIFM_UT_Query_Register.xlsx"


settings = AppSettings()
settings.paths.ensure_exist()
