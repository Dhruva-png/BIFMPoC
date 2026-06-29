# BIFM Unit Trusts — Local Document Processing (POC)

A fully offline desktop application implementing the BIFM UT POC Understanding &
Alignment Document v1.0: form classification, field extraction, validation,
Excel reporting, and document filing — built against your **local Ollama**
install only (`llama3.2` for reasoning/classification, `llava` for vision/OCR).
No cloud APIs, no external services.

## What this implements (mapped to the alignment document)

| Section | Implemented as |
|---|---|
| 3. Form Inventory | `config/form_types.json` |
| 4. Field Extraction & Validation Matrix | `config/field_definitions.json`, `config/validation_rules.json` |
| 5. Output Format (4-sheet Excel) | `app/services/report_generator.py` |
| 5.3 Naming convention | `app/services/filer.py` |
| 6. Confidence thresholds | `config/validation_rules.json` → `confidence_thresholds`, used in `app/core/pipeline.py` |
| 7.2 Module 1 — Form Classifier | `app/services/classifier.py` |
| 7.2 Module 2 — Field Extractor | `app/services/extractor.py` |
| 7.2 Module 3 — Validation Engine | `app/validation/engine.py` (pure Python, no LLM calls) |
| 7.2 Module 4 — Excel Report Generator | `app/services/report_generator.py` |
| 7.2 Module 5 — Document Filer | `app/services/filer.py` |

**Out of scope per Section 1.1 ("What This POC Is NOT")**, and therefore *not*
implemented: AWD core integration, live SharePoint write-back (documents are
filed to a local `output/filed_documents` folder using the exact naming
convention instead — swapping in the SharePoint Graph API later only touches
`filer.py`), the Botswana Life payslip use case, and end-to-end disinvestment
automation.

## Why only two models, and why so few LLM calls per form

- **llama3.2** isn't actually called anywhere in the current pipeline — all
  classification/extraction reasoning in this POC is visual (forms are
  scanned PDFs), so it goes through `llava`. The `ask_text()` function in
  `app/llm/ollama_client.py` is wired up and ready if you want to add a
  text-reasoning step later (e.g. cross-checking extracted text against
  business rules in natural language).
- Everything **deterministic** (ID digit checks, gender-from-Omang, date
  logic, email regex, percentage totals, file naming) runs in plain Python —
  zero LLM calls, 100% reproducible, and it's what makes the "100% rule
  coverage" success criterion in Section 9 achievable.
- A typical Individual Investment Application Form costs **1 llava call**
  (classification) **+ 1 llava call** (field extraction) = 2 calls. Minor/
  guardian accounts add **1 more call** for the Page 10 section, and only
  for forms where it's actually needed. The GSG-vs-Standard disinvestment
  disambiguation only runs when the first classification call comes back
  below the "high confidence" threshold — not on every form.

## Setup (Windows)

1. **Install Ollama**: https://ollama.com/download — installs as a background service.
2. **Pull the two models** (one-time, several GB download):
   ```
   ollama pull llama3.2
   ollama pull llava
   ```
3. **(Optional) Install Tesseract OCR** — only used as a cheap keyword hint for page-finding, not required:
   - https://github.com/UB-Mannheim/tesseract/wiki
4. **Install Python 3.11+**, then from this project folder:
   ```
   pip install -r requirements.txt
   ```

> **Note on PDF rendering / "Is poppler installed?"**: earlier versions of this
> POC used `pdf2image`, which shells out to the external Poppler binaries
> (`pdftoppm`/`pdftocairo`) — a separate, non-pip install that has to be put on
> PATH by hand, and is exactly what produced the "Unable to get page count.
> Is poppler installed and in PATH?" error. The app now renders PDFs with
> **PyMuPDF**, which bundles its own renderer inside the pip wheel, so step 4
> above is the only install needed — there's nothing extra to download or add
> to PATH.

## Running

There are two front ends. Both call the exact same pipeline in `app/`.

**Streamlit dashboard (recommended)** — drag-and-drop uploads, live progress,
a results dashboard with status badges, per-document field/validation
drill-down, and a one-click Excel download:
```
streamlit run streamlit_app.py
```

**Original Tkinter desktop GUI / headless batch:**
```
# Desktop GUI
python main.py

# Headless batch (e.g. for scripting/scheduling)
python main.py --headless --intake "C:\path\to\intake\folder"
```

Drop PDFs into the `intake/` folder (or pick a folder/upload in the UI), click
**Run Batch**. Results:
- Consolidated report: `output/BIFM_UT_Processing_Report.xlsx`
- Filed/renamed documents: `output/filed_documents/`
- Logs: `logs/bifm_app.log`

## Configuration

Nothing is hard-coded. To change models, host, or DPI, set environment
variables before launching, or edit `config/settings.py`:

```
set OLLAMA_HOST=http://localhost:11434
set OLLAMA_TEXT_MODEL=llama3.2
set OLLAMA_VISION_MODEL=llava
```

To add/change a form type, field, or validation rule, edit the relevant JSON
file under `config/` — no code changes required, per Section 7.3's
"configurable JSON rule set" recommendation.

## Testing

The validation engine and file-naming logic are pure Python and fully unit
tested without needing Ollama running:

```
pytest tests/ -v
```

(18 tests, all passing — covers Omang/Birth-Cert digit validation, gender
derivation from the 5th digit, ID expiry/Birth-Certificate skip logic, minor/
guardian conditional requirements, preference defaults, beneficiary
percentage totals, and the exact naming-convention examples from Section 5.3.)

## Known assumptions (documented per "If Information Is Missing")

1. **SharePoint filing** → local folder + exact naming convention (live
   SharePoint API explicitly out of POC scope, Section 1.1).
2. **`id_type` derivation** — the field extractor relies on llava correctly
   identifying which ID-type checkbox is ticked on the form. If your actual
   form layout differs from the description in Section 4, you may need to
   adjust the field labels/hints in `config/field_definitions.json`.
3. **GSG sample form** (Action Item A1, not yet received per the document) —
   the classifier's GSG-distinguishing prompt is written from the document's
   description ("lists only the GSG fund") rather than a real sample. Re-test
   `classifier.disambiguate_gsg_vs_standard()` once that sample arrives.
4. **Date formats** — the validator accepts `YYYY-MM-DD`, `DD/MM/YYYY`,
   `DD-MM-YYYY`, and `DD Month YYYY`. Add more to `DATE_FORMATS` in
   `app/validation/engine.py` if the actual forms use something else.

## Project layout

```
main.py                        Entry point (Tkinter GUI or --headless)
streamlit_app.py                Streamlit dashboard front end (recommended)
config/
  settings.py                  Paths, model names, host, DPI — no hard-coding
  form_types.json              Section 3 — 6 form types + fund list
  field_definitions.json       Section 4.1 — field matrix
  validation_rules.json        Section 4.2 — rule logic + mandatory fields + confidence thresholds
app/
  llm/ollama_client.py         Only module that talks to Ollama
  ocr/pdf_utils.py             PDF -> page images (PyMuPDF, + optional Tesseract hint scan)
  models/schemas.py            Dataclasses shared across the pipeline
  services/
    classifier.py              Module 1
    extractor.py                Module 2
    report_generator.py        Module 4
    filer.py                   Module 5
  validation/engine.py         Module 3 (pure Python, no LLM)
  core/pipeline.py             Orchestrates Modules 1-5 per batch
  ui/main_window.py            Tkinter desktop GUI
tests/                         pytest suite (validation + naming)
intake/                        Drop source PDFs here
output/                        Excel report + filed_documents/
logs/                          Rotating log file
```
