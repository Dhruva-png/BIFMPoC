# BIFM Unit Trusts — Document Processing (POC)

AI-powered pipeline for BIFM UT instruction form processing. Runs against
either a **free cloud vision model (default)** or a **fully local/offline
model** — pick whichever suits your hardware.

## Quick start: choosing an LLM provider

Running an 8B vision model locally needs a real GPU (or a lot of patience on
CPU). If your laptop can't handle that, use the free Gemini API instead —
same code, same output, just faster and with zero local model download.

### Option A — Gemini (default, recommended for laptops)

1. Get a free API key: https://aistudio.google.com/apikey (any Google
   account, no credit card, no charge on the free tier).
2. Set it as an environment variable before launching:
   ```bash
   # macOS/Linux
   export GEMINI_API_KEY="your-key-here"

   # Windows (Command Prompt)
   set GEMINI_API_KEY=your-key-here

   # Windows (PowerShell)
   $env:GEMINI_API_KEY="your-key-here"
   ```
3. Run the app as normal (`python main.py`, or `streamlit run streamlit_app.py`).
   `LLM_PROVIDER` defaults to `gemini`, so nothing else changes.

Free-tier limits are generous for a POC (gemini-2.0-flash: roughly 15
requests/minute, 1,500/day at no cost — check the AI Studio dashboard for
current numbers). Typical latency is 1-3 seconds per page, vs. minutes on a
CPU-only laptop running the local model.

### Option B — Ollama (fully local/offline)

If you'd rather keep everything on-device (no internet dependency, no data
leaving the machine), set:
```bash
export LLM_PROVIDER=ollama
```
and follow the local setup below (`ollama serve` + pull the models). This is
the original setup this POC shipped with — nothing about it changed, it's
just no longer the default.

Both providers produce identical output (same JSON shape, same downstream
pipeline) — `app/llm/router.py` is the single switch point, so business
logic never needs to know which one is active.

## What it does

Processes scanned PDF instruction forms through 5 stages:

| Stage | Module | What happens |
|-------|--------|-------------|
| 1 | Classifier | Identifies the form type from page 1 image |
| 2 | Extractor | Reads form fields using a vision LLM (qwen3-vl / llava) |
| 3 | Validator | Checks mandatory fields, ID formats, dates, branch codes |
| 4 | Report | Builds Excel workbook (4 sheets) |
| 5 | Filer | Copies PDF with standardized filename to `output/filed_documents/` |

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

# 2. Start Ollama
ollama serve

# 3. Pull the required models
ollama pull qwen3-vl:8b-instruct   # vision model (form classification + extraction)
ollama pull llama3.2               # text model (fallback reasoning)
```

### Run the app

```bash
streamlit run streamlit_app.py
```

Open http://localhost:8501. Upload PDFs in the sidebar and click **Run Batch**.

### Environment variables (optional overrides)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_VISION_MODEL` | `qwen3-vl:8b-instruct` | Vision model for classification & extraction |
| `OLLAMA_TEXT_MODEL` | `llama3.2` | Text model |
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
│   └── validation_rules.json     # Validation rule definitions
├── app/
│   ├── core/pipeline.py          # Main orchestration (Module 1-5)
│   ├── llm/ollama_client.py      # LLM calls (ask_text, ask_vision)
│   ├── ocr/pdf_utils.py          # PDF → image rendering (pypdfium2)
│   ├── services/
│   │   ├── classifier.py         # Module 1: form type classification
│   │   ├── extractor.py          # Module 2: field extraction (all 6 forms)
│   │   ├── report_generator.py   # Module 4: Excel report (4 sheets)
│   │   └── filer.py              # Module 5: document filing + naming
│   ├── validation/engine.py      # Module 3: deterministic rule validation
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
| **Investor Master** | One row per document — all extracted fields plus derived metadata (form_type, fund_category, processing_cutoff). Priority columns shown first. Rows colour-coded by validation status. |
| **Beneficiary Details** | Beneficiary name, relationship, split % (APPFORM only) |
| **Validation Flags** | Every validation check that didn't PASS — field, value, status, message |
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
