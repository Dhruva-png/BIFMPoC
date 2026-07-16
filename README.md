# BIFM Unit Trusts — Document Processing (POC)

AI-powered pipeline for BIFM UT instruction form processing. Runs against
**Groq's free cloud vision model** — no local GPU, no model download, no
credit card.

## Quick start: set your Groq API key

> **Tip — don't re-enter the key every session:** copy `.env.example` to
> `.env` and fill your key in there instead of `export`ing it each time.
> `.env` is loaded automatically on startup and is gitignored, so real keys
> never get committed. Environment variables still work too — `.env` is just
> a persistent alternative.

1. Get a free API key: https://console.groq.com/keys (email or Google
   sign-in, no credit card, no charge on the free tier).
2. Set `GROQ_API_KEY` in `.env` (see tip above), or as an environment
   variable before launching:
   ```bash
   # macOS/Linux
   export GROQ_API_KEY="your-key-here"

   # Windows (Command Prompt)
   set GROQ_API_KEY=your-key-here

   # Windows (PowerShell)
   $env:GROQ_API_KEY="your-key-here"
   ```
3. Run the app (`python main.py`, or `streamlit run streamlit_app.py`).

Free-tier limits are generous for a POC (on the order of ~15-30 requests/
minute and several thousand/day — check the Groq console dashboard for
current numbers). Rate limits apply at the organization level, not per key.
The app self-throttles below your account's TPM ceiling (`GROQ_TPM_LIMIT`)
so concurrent workers queue instead of tripping 429s.

`app/llm/router.py` is the single point the rest of the app talks to, so
business logic never calls the backend directly.


## What it does

Processes scanned PDF instruction forms through 5 stages:

| Stage | Module | What happens |
|-------|--------|-------------|
| 1 | Classifier | Identifies the form type from page 1 image |
| 2 | Extractor | Reads form fields using Groq's vision LLM |
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

# 2. Set your Groq API key (see "Quick start" above)
#    Either copy .env.example to .env and fill in GROQ_API_KEY,
#    or export it:
export GROQ_API_KEY="your-key-here"     # Windows PowerShell: $env:GROQ_API_KEY="..."
```

### Run the app

```bash
streamlit run streamlit_app.py
```

Open http://localhost:8501. Upload PDFs in the sidebar and click **Run Batch**.

### Environment variables (optional overrides)

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | (none) | **Required** — the app's LLM backend |
| `GROQ_VISION_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Vision model for classification & extraction |
| `GROQ_TEXT_MODEL` | `llama-3.3-70b-versatile` | Text model |
| `GROQ_TPM_LIMIT` | `27000` | Self-throttle budget, kept below your account's real TPM ceiling |
| `PDF_RENDER_DPI` | `220` | DPI for PDF-to-image rendering |
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
│   ├── llm/groq_client.py        # LLM calls (ask_text, ask_vision)
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

- Live SharePoint read/write (Graph API integration)
- AWD system integration
- Multi-user access control
- Document ingestion from email (mailbox polling)

These are production-phase additions. The naming convention, metadata structure,
and Excel output format are designed for direct handoff to a SharePoint/Graph API
integration layer.
