# BIFM Unit Trusts — Document Processing (POC)

AI-powered pipeline for BIFM UT instruction form processing. Runs against
**Google Gemini** — no local GPU, no model download, no credit card.
(Earlier versions of this POC ran on Groq, then briefly on OpenRouter;
OpenRouter was dropped after a real batch run found its free-tier shared
pool throwing 429s caused by contention from *other* OpenRouter users,
not this account's own quota — confirmed live via OpenRouter's own
`/api/v1/key` endpoint showing `usage_daily: 0` at the exact moment
requests were failing. Gemini talks to Google directly with its own key
and quota, no shared pool to contend with. See
`app/llm/gemini_client.py`'s docstring for the full story.)

## Quick start: set an API key

> **Tip — don't re-enter the key every session:** copy `.env.example` to
> `.env` and fill your key in there instead of `export`ing it each time.
> `.env` is loaded automatically on startup and is gitignored, so real keys
> never get committed. Environment variables still work too — `.env` is just
> a persistent alternative.

1. Get a free API key: https://aistudio.google.com/apikey (any Google
   account, no card required).
2. Set `GEMINI_API_KEY` in `.env` (see tip above), or as an environment
   variable before launching:
   ```bash
   # macOS/Linux
   export GEMINI_API_KEY="your-key-here"

   # Windows (Command Prompt)
   set GEMINI_API_KEY=your-key-here

   # Windows (PowerShell)
   $env:GEMINI_API_KEY="your-key-here"
   ```
3. Run the app (`python main.py`, or `streamlit run streamlit_app.py`).

Optional second key: both use cases process documents concurrently, so a
normal-sized batch can trip one key's free-tier rate limit. Set
`GEMINI_API_KEY_2` (a second key, same way) and requests round-robin
across both, rotating off a rate-limited key instead of waiting it out —
see `app/llm/gemini_client.py`'s docstring. Not required; one key still
works.

Default model is `gemini-3.1-flash-lite` — checked against Google's own
model list before picking it (Stable, multimodal: text/image/video/audio/
PDF input, positioned by Google as their fast/cheap tier for lightweight
data-extraction tasks — a good match for this app's per-page
vision-extraction calls).

Some newer Google Cloud projects have a **0 free-tier quota** for Gemini
(often region-restricted) — if `check_connection()` fails with a quota
error, that project needs billing enabled. `app/llm/router.py` is the
single point the rest of the app talks to; it never calls
`app/llm/gemini_client.py` directly, so if the backend ever needs to
change again, only those two files do.


## Two applications, one platform

This repo contains **two separate applications** sharing one LLM/OCR
foundation (`app/llm`, `app/ocr`, the IMAP connector, settings):

| Use case | App | Entry points |
|---|---|---|
| **UC1 — BIFM UT**: Instruction form processing & workflow automation | `app/` | `streamlit run streamlit_app.py` · `python main.py` |
| **UC2 — BIFM Ops**: Withdrawal & Contribution audit document automation | `ops/` | `streamlit run ops_app.py` · `python ops_main.py` |

## Use Case 2 — BIFM Ops: Audit Document Automation

Automates intake, correlation, and audit compilation for the Ops /
Investment team's Withdrawal and Contribution transactions:

1. **Intake** — the configured email mailbox (same `IMAP_*` settings), a
   SharePoint input folder (`SHAREPOINT_OPS_*` settings — see "SharePoint
   Folder Structure — Use Case 2" below), exported `.eml` emails, or a
   drop folder. Email bodies are themselves audit documents (the
   Investment Team Approval Email) and are captured alongside PDF
   attachments.
2. **Classification & metadata extraction** — one combined LLM call per
   document classifies it (Withdrawal Instruction, Deposit Confirmation,
   Proof of Payment, Trade Order, Bank Statement, …) and extracts the
   metadata table: Portfolio Code/Name, Client Name, Transaction &
   Instruction Type, Transaction/Trade/Email dates, Amount, Security
   Name/Code, Trade ID, Document Type/Source. The client-provided
   **Portfolio Name→Code mapping** (`ops/config/portfolio_mapping.json`,
   or `OPS_PORTFOLIO_MAPPING=<client csv>`) resolves loosely-written
   portfolio names to standardized codes — unambiguous matches only.
3. **Routing** — instructions are routed to the Investment or Operations
   team per `ops/config/ops_workflow.json`.
4. **Correlation** — deterministic grouping into transactions: Trade ID
   first, then portfolio+amount within a date window; uncorrelatable
   documents are flagged for review, never force-merged. Some of BIFM's
   real documents cover several portfolios in one file (a shared daily
   Cash/Trade Order batch report, or a letter instructing on multiple of
   a client's own portfolios at once) — `ops/analyzer.py` extracts each
   row separately when a document is genuinely that kind of multi-
   portfolio ledger, and `ops/correlator.py` attaches a per-transaction
   copy of the same physical document to every transaction one of its
   rows actually matches (see that module's docstring for the full
   design).
5. **Filing** — `ops_output/filed/<Year>/<Month>/<Date>/<Transaction>/`.
6. **Audit packs** — evaluated against the proposal's pack composition
   (withdrawals: instruction, approval email, trade order, cash flow,
   bank statement/POP; contributions: POP/deposit confirmation, bank
   statement, proof of transfer, cash & trade templates) and compiled
   into `ops_output/audit_repository/<PortfolioCode>/<configurable name>/`
   with a MANIFEST.txt stating exactly what is present and missing.
7. **Search** — a persistent SQLite metadata repository; search any
   extracted field from the UI (`ops_app.py` → Search tab) or CLI
   (`python ops_main.py search "TRD-2026-001"`).
8. **Filter / export by salesperson** *(demo capability)* — there is no
   SharePoint/HR-directory integration for salesperson assignment yet, so
   this uses a synthetic roster (`ops/config/salesperson_roster.json`,
   portfolio → salesperson) as a stand-in, purely so the capability can be
   demonstrated end-to-end. A document that actually states its own
   advisor/consultant/broker name always overrides the roster — the
   roster is only a fallback for documents that don't say. Swap in a real
   feed later via `OPS_SALESPERSON_ROSTER=<client csv>` (same override
   pattern as the portfolio mapping) without touching code. Both UI tabs
   get a "Filter by salesperson" dropdown, plus a scoped "Download audit
   packs for `<name>`" zip; CLI equivalents:
   `python ops_main.py transactions --salesperson "Thabo Kgosi"` and
   `python ops_main.py export-packs "Thabo Kgosi" -o packs.zip`.

## Use Case 1 — What it does

Processes scanned PDF instruction forms through 5 stages:

| Stage | Module | What happens |
|-------|--------|-------------|
| 1 | Classifier | Identifies the form type from page 1 image |
| 2 | Extractor | Reads form fields using the configured vision LLM (Gemini) |
| 3 | Validator | Checks mandatory fields, ID formats, dates, branch codes |
| 3b | Pre-Validation Flags | Rejection-risk checklist per Section 8 of the understanding doc (see below) |
| 4 | Report | Builds Excel workbook (7 sheets) |
| 5 | Filer | Copies PDF into the per-form-type SharePoint folder structure (Section 6), under a standardized filename |

## Pre-Validation Flags (Section 8)

Separate from field-level validation, `app/validation/prevalidation.py` runs a
config-driven checklist (`config/prevalidation_flags.json`) of the exact
rejection-risk signals BIFM's authorisers flagged in the requirement
sessions — page initials, signature, banking completeness, fund selection,
KYC/companion-document presence, beneficiary completeness/split, debit
timing, GSGF quarterly cut-off reminders, and more. Each flag carries the
rejection risk BIFM assigned it (High / Medium / Process reminder / Process
impact) and is written to its own **Pre-Validation Flags** sheet — every
applicable flag per document, triggered or not, colour-coded so Ops can
triage High-risk items first. Companion-document presence (e.g. a KYC form
dropped in alongside a New Business form) is detected from the other
documents in the same batch, since KYC/POP are filed as companion documents
rather than standalone instructions (Section 9.1).

A couple of flags (e.g. per-page "initials present") reference signals the
vision-LLM extractor doesn't yet produce; these are reported as "not
verified" rather than a false trigger — see the module docstring.

## SharePoint Folder Structure (Section 6)

`app/services/filer.py` routes each filed document through the **exact**
per-form-type structure confirmed in the understanding document — the six
instruction types do not share one generic folder tree:

| Form | Structure |
|------|-----------|
| New Business | `Submissions` → `Received` → `Approved/<Year>/<Month>/<Day>` \| `Rejected` (flat) |
| Additional Investment | `<Year>/<Month>` (dump, awaiting e-stamp) → `<Day>/MoneyMarket\|NonMoneyMarket/Captured[/Approved\|Rejected]` |
| Disinvestment (Standard) | `<Year>/<Month>/<Day>/Money Market\|Non-Money Market/Captured\|Approved\|Rejected` |
| Disinvestment (GSGF) | `<Year>/Q<n>-<Year>/<quarter-end date>/Captured\|Approved\|Rejected` (quarterly, not daily) |
| Debit Order | `<Year>/<Month>` (dump) → `Send to AWD/<Day>` \| `Rejected/<Day>` |
| Static (Change of Details) | `<Year>/<Month>` (dump) → `Send to AWD/<Day>` \| `Rejected/<Day>` |
| KYC | `<Year>/<Month>/<Day>` (companion document, not a standalone instruction) \| `Rejected` |

## SharePoint Folder Structure — Use Case 2 (Ops)

UC2 shares its SharePoint connection (`app/connectors/sharepoint_client.py`,
Microsoft Graph app-only auth) with UC1, but reads and writes a **separate**
set of folders — configure both sections' `SHAREPOINT_*` variables (see
`.env.example`) even though they point at the same tenant/site/drive.
Create these folders on the real SharePoint document library before
pointing the app at it (the app does not create folders itself — only
files inside them):

| Purpose | Env var | Default path | What goes here |
|---|---|---|---|
| **Input** (submissions) | `SHAREPOINT_OPS_SUBMISSION_FOLDER` | `Ops Instructions/Submissions` | Where Ops/clients drop incoming Withdrawal & Contribution documents — PDFs, and exported `.eml`/`.txt` files. This is the folder UC2 polls when you pick **SharePoint folder** as the source (UI) or pass `--sharepoint` (CLI). |
| **Processed** | `SHAREPOINT_OPS_PROCESSED_FOLDER` | `Ops Instructions/Submissions/Processed` | Each submission item is **moved** here immediately after download (not deleted), so a repeat run never reprocesses it into a duplicate transaction — the SharePoint equivalent of IMAP marking a message `\Seen`. Set `SHAREPOINT_OPS_MARK_PROCESSED=false` to leave items in place instead (useful while repeatedly testing against the same file). |
| **Filed documents** | `SHAREPOINT_OPS_FILED_FOLDER` | `Ops Filed Documents` | Mirrors the local `ops_output/filed/<Year>/<Month>/<Day>/<Transaction>/` structure at the identical relative path, one file at a time as each document is filed. Only written when `SHAREPOINT_OPS_WRITE_BACK=true`. |
| **Audit repository** | `SHAREPOINT_OPS_AUDIT_FOLDER` | `Ops Audit Repository` | Mirrors the local `ops_output/audit_repository/<PortfolioCode>/<Transaction folder>/` structure (documents + `MANIFEST.txt`), one compiled pack at a time. Only written when `SHAREPOINT_OPS_WRITE_BACK=true`. |

Only the **Input** folder needs to exist up front. **Filed documents**
and **Audit repository** are only touched once
`SHAREPOINT_OPS_WRITE_BACK=true` is set — until then, filing and audit
compilation stay local-only (`ops_output/`), same as the demo has been
running so far.

## Supported Form Types

All 6 BIFM UT form types are fully classified, extracted, validated, and filed:

| Code | Form Name | Fund Category Logic |
|------|-----------|---------------------|
| `APPFORM` | Investment Application Form | N/A (new investor) |
| `ADD` | Additional Investment Form | MM or NMM derived from fund name |
| `DEBIT` | Debit Order Form | MM or NMM derived from fund name |
| `DIS` | Disinvestment Form (Standard) | MM or NMM derived from fund name |
| `DIS_GSG` | Disinvestment Form (GSGF) | Always NMM-GSGF, quarterly cut-off |
| `STATIC` | Static / Change of Investor Details | N/A |
| `KYC` | KYC (Know Your Customer) | N/A |

## System-Derived Metadata Fields

The system generates these fields automatically (per requirements doc) — they do **not** need to appear on the form:

| Field | Source |
|-------|--------|
| `form_type` | Derived from classification |
| `fund_category` | Money Market / Non-Money Market / Non-Money Market (GSGF) |
| `processing_cutoff` | 12:00 PM (MM) / 3:00 PM (NMM) / Quarterly (GSGF) |
| `instruction_mode` | Partial Amount / Percentage / Full Closure (DIS only) |
| `sub_instruction_type` | Cancel / Change / New (DEBIT only) |
| `static_sub_type` | Change of Personal Details / Update Banking Details (STATIC only) |
| `kyc_completeness_flag` | Complete / Incomplete (N/4 documents provided) (KYC only) |

## Quick Start

### Prerequisites

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Set your API key (see "Quick start" above)
#    Either copy .env.example to .env and fill in the key, or export:
export GEMINI_API_KEY="your-key-here"     # Windows PowerShell: $env:GEMINI_API_KEY="..."
```

### Run the app

```bash
streamlit run streamlit_app.py
```

Open http://localhost:8501. Upload PDFs in the sidebar and click **Run Batch**.

### Environment variables (optional overrides)

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | (none) | **Required** — the app's only LLM backend |
| `GEMINI_API_KEY_2` | (none) | Optional second key — requests round-robin across both for higher throughput |
| `GEMINI_VISION_MODEL` | `gemini-3.1-flash-lite` | Vision model for classification & extraction |
| `GEMINI_TEXT_MODEL` | `gemini-3.1-flash-lite` | Text model |
| `PDF_RENDER_DPI` | `220` | DPI for PDF-to-image rendering |
| `OCR_DESKEW` | `true` | Straighten tilted scans before reading (local, ~100ms/page) |
| `RETRY_MISSING_MANDATORY` | `true` | One focused re-read when mandatory fields come back blank |
| `BIFM_MAX_WORKERS` | `2` | Max parallel documents |
| `LOG_LEVEL` | `INFO` | Logging level |


## Project Structure

```
bifm_ocr_app/
├── streamlit_app.py              # UI entry point
├── main.py                       # CLI entry point
├── requirements.txt
├── config/
│   ├── settings.py               # All tunables (env-overridable)
│   ├── form_types.json           # Form codes, classification hints, fund info
│   ├── field_definitions.json    # Field lists for ALL 6 form types
│   ├── validation_rules.json     # Field-level validation rule definitions
│   └── prevalidation_flags.json  # Section 8: rejection-risk flag definitions
├── app/
│   ├── core/pipeline.py          # Main orchestration (Module 1-5)
│   ├── llm/gemini_client.py      # LLM calls (ask_text, ask_vision) - the only backend
│   ├── llm/router.py             # Single entry point the app imports
│   ├── ocr/pdf_utils.py          # PDF → image rendering (pypdfium2)
│   ├── services/
│   │   ├── classifier.py         # Module 1: form type classification
│   │   ├── extractor.py          # Module 2: field extraction (all 6 forms)
│   │   ├── report_generator.py   # Module 4: Excel report (4 sheets)
│   │   └── filer.py              # Module 5: document filing + naming
│   ├── validation/
│   │   ├── engine.py             # Module 3: deterministic field-level validation
│   │   └── prevalidation.py      # Section 8: rejection-risk flags engine
│   ├── models/schemas.py         # Shared dataclasses
│   └── utils/
│       ├── config_loader.py      # JSON config loaders + fund category lookup
│       └── logger.py             # Structured logging
├── intake/                       # Drop PDFs here (or upload via UI)
├── output/
│   ├── filed_documents/          # Filed PDFs with standardized names
│   └── BIFM_UT_Processing_Report.xlsx
└── logs/
    └── bifm_app.log
```

## Excel Report Sheets

| Sheet | Contents |
|-------|----------|
| **Consolidated Investor Profile** | One row per batch/person — merged identity/contact/banking fields backfilled across every document that person submitted together |
| **Investor Master** | One row per document — all extracted fields plus derived metadata (form_type, fund_category, processing_cutoff). Priority columns shown first. Rows colour-coded by validation status. |
| **Beneficiary Details** | Beneficiary name, relationship, split % (APPFORM only) |
| **Validation Flags** | Every field-level validation check that didn't PASS — field, value, status, message |
| **Pre-Validation Flags** | Section 8 rejection-risk checklist — every applicable flag per document, triggered or not, with rejection risk and reason |
| **Confidence Scores** | Per-field extraction confidence (Section 7), colour-coded by the same thresholds used for auto-accept/flag/escalate |
| **Processing Log** | Timestamp, original filename, standardized new filename, form type, confidence, status |

## Document Naming Convention

Filed documents are named: `[FormType]_[EntityNumber]_[InvestorSurname]_[YYYYMMDD].pdf`

Examples:
- `ADD_ENT12345_MODISE_20260630.pdf`
- `DIS_GSG_ENT67890_TAU_20260630.pdf`
- `KYC_ENT11111_SETLHARE_20260630.pdf`

## Configuration-Driven Design

All form types, field lists, and validation rules are JSON — no code changes needed to:
- Add a new field to an existing form → edit `config/field_definitions.json`
- Add a new validation rule → edit `config/validation_rules.json`
- Update fund cut-off times → edit `config/form_types.json`

## What This POC Does NOT Include

- AWD system integration
- Multi-user access control

SharePoint read/write (`app/connectors/sharepoint_client.py`, Microsoft
Graph app-only auth) and email mailbox polling (IMAP) are both implemented
as optional connectors for both use cases — see "SharePoint Folder
Structure" above and `.env.example` for the `SHAREPOINT_*` / `IMAP_*`
settings that turn them on. AWD integration and multi-user access control
remain production-phase additions. The naming convention, metadata
structure, and Excel output format were designed from the start for direct
handoff to a SharePoint/Graph API integration layer.
