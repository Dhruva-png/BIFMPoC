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
class GroqSettings:
    """
    Groq's free, no-credit-card cloud API - the LLM backend this app runs
    on. Get a key at https://console.groq.com/keys (email or Google
    sign-in, no card) and set it as GROQ_API_KEY.
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
    groq: GroqSettings = field(default_factory=GroqSettings)
    sharepoint: SharePointSettings = field(default_factory=SharePointSettings)
    imap: ImapSettings = field(default_factory=ImapSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    pdf_render_dpi: int = int(os.environ.get("PDF_RENDER_DPI", "220"))
    ocr_enhance_images: bool = os.environ.get("OCR_ENHANCE_IMAGES", "true").lower() != "false"
    ocr_min_long_edge_px: int = int(os.environ.get("OCR_MIN_LONG_EDGE_PX", "1600"))
    # Straightens tilted scans (projection-profile deskew, pure local math,
    # ~100ms/page, no API cost) before enhancement. OCR_DESKEW=false to skip.
    ocr_deskew: bool = os.environ.get("OCR_DESKEW", "true").lower() != "false"
    # When a document's MANDATORY fields come back blank after the normal
    # read, re-ask once with a focused prompt listing only those fields
    # (narrower prompts get more attention per field). Costs at most ONE
    # extra vision call per document, and only on documents that would
    # otherwise auto-reject for missing mandatory data - a clean batch pays
    # nothing. RETRY_MISSING_MANDATORY=false to disable.
    retry_missing_mandatory: bool = os.environ.get("RETRY_MISSING_MANDATORY", "true").lower() != "false"
    # Used at TWO levels in app.core.pipeline.run_batch: up to this many
    # different PEOPLE's batches run concurrently, and within each of
    # those, up to this many of that person's own documents run
    # concurrently - so at 3 (the default), a multi-person folder can have
    # up to 9 documents genuinely in flight at once. Each document mostly
    # just waits on an LLM HTTP call, so this is I/O-bound concurrency, not
    # CPU parallelism - the real ceiling is the LLM provider's rate limit,
    # which for Groq is enforced separately and safely by the proactive
    # TPM limiter in app.llm.groq_client (workers queue on it rather than
    # ever exceeding the account's real budget), so raising this number is
    # safe to try even before knowing your exact tier's headroom.
    max_workers: int = int(os.environ.get("BIFM_MAX_WORKERS", "3"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    excel_report_name: str = "BIFM_UT_Processing_Report.xlsx"
    query_register_report_name: str = "BIFM_UT_Query_Register.xlsx"


settings = AppSettings()
settings.paths.ensure_exist()
