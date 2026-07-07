"""
BIFM Unit Trusts — Document Processing (POC)
Streamlit front end.

Run with:
    streamlit run streamlit_app.py

Supports all 6 BIFM UT form types through the full pipeline:
  APPFORM  Investment Application Form
  ADD      Additional Investment Form
  DEBIT    Debit Order Form
  DIS      Disinvestment Form (Standard)
  DIS_GSG  Disinvestment Form (GSGF)
  STATIC   Static / Change of Investor Details
  KYC      KYC (Know Your Customer)

Every form is classified, field-extracted, validated, and filed.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core.pipeline import DocumentOutcome, process_batch, process_single_document
from app.llm.router import check_connection, active_provider
from app.models.schemas import ValidationStatus
from app.services import intake
from app.services.report_generator import ExcelReportBuilder
from app.services.query_register import QueryRegisterBuilder
from app.utils.config_loader import load_form_types
from config.settings import settings

# --------------------------------------------------------------------------- #
# Page config & theme
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="BIFM Unit Trusts — Document Processing",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

PRIMARY = "#0F4C81"
ACCENT  = "#C9A24B"
PASS_C  = "#1E8E5A"
WARN_C  = "#B7791F"
FAIL_C  = "#C0392B"
BG_CARD = "#FFFFFF"
INK     = "#1B2733"
MUTED   = "#5C6B7A"

st.markdown(
    f"""
    <style>
    .stApp {{ background: linear-gradient(180deg, #F4F7FB 0%, #EEF2F7 100%); }}
    [data-testid="stSidebar"] {{ background: {INK}; }}
    [data-testid="stSidebar"] * {{ color: #E7ECF2 !important; }}
    [data-testid="stSidebar"] hr {{ border-color: rgba(255,255,255,0.15); }}

    /* Streamlit's own alert boxes (st.success/error/warning/info) keep their
       light pastel backgrounds even inside the dark sidebar — force dark
       text back on just those so it doesn't inherit the light sidebar text
       color and disappear against them. */
    [data-testid="stSidebar"] [data-testid="stAlert"],
    [data-testid="stSidebar"] [data-testid="stAlertContainer"],
    [data-testid="stSidebar"] .stAlert {{
        color: {INK} !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlert"] *,
    [data-testid="stSidebar"] [data-testid="stAlertContainer"] *,
    [data-testid="stSidebar"] .stAlert * {{
        color: {INK} !important;
    }}

    .bifm-header {{
        display:flex; align-items:center; gap:14px;
        padding: 18px 26px; margin-bottom: 8px;
        background: linear-gradient(120deg, {PRIMARY} 0%, #133E66 100%);
        border-radius: 14px; color: white;
        box-shadow: 0 6px 18px rgba(15,76,129,0.25);
    }}
    .bifm-header h1 {{ font-size: 1.45rem; margin:0; font-weight:700; letter-spacing:.2px; }}
    .bifm-header p {{ margin:0; color:#D7E4F2; font-size:0.85rem; }}
    .bifm-badge {{
        background: rgba(255,255,255,0.14); color:#fff; font-size:0.72rem;
        padding: 3px 10px; border-radius:999px; border:1px solid rgba(255,255,255,0.25);
        margin-left:auto; white-space:nowrap;
    }}

    .metric-card {{
        background:{BG_CARD}; border-radius:14px; padding:16px 18px;
        border:1px solid #E3E8EE; box-shadow:0 2px 8px rgba(15,23,42,0.04);
    }}
    .metric-card .label {{ color:{MUTED}; font-size:0.78rem; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }}
    .metric-card .value {{ color:{INK}; font-size:1.9rem; font-weight:700; line-height:1.2; }}

    .status-chip {{
        display:inline-block; padding:3px 11px; border-radius:999px;
        font-size:0.74rem; font-weight:700; letter-spacing:.02em;
    }}
    .chip-pass    {{ background:#E4F6EC; color:{PASS_C}; }}
    .chip-warning {{ background:#FCF1DD; color:{WARN_C}; }}
    .chip-fail    {{ background:#FBE6E4; color:{FAIL_C}; }}
    .chip-error   {{ background:#FBE6E4; color:{FAIL_C}; }}

    .section-title {{ font-size:1.02rem; font-weight:700; color:{INK}; margin: 6px 0 10px 0; }}
    div[data-testid="stExpander"] {{ background:{BG_CARD}; border-radius:12px; border:1px solid #E3E8EE; }}

    /* Sidebar expanders: dark background, light text */
    [data-testid="stSidebar"] div[data-testid="stExpander"] {{
        background: #263342 !important;
        border-color: rgba(255,255,255,0.12) !important;
    }}
    [data-testid="stSidebar"] div[data-testid="stExpander"] * {{
        color: #E7ECF2 !important;
    }}
    [data-testid="stSidebar"] details summary p {{
        color: #E7ECF2 !important;
        font-weight: 600;
    }}

    /* Sidebar form widgets (selectbox, radio, text input, file uploader) —
       these are BaseWeb components with their own light backgrounds, so the
       global white text rule above made them unreadable (white-on-white).
       Give them a dark surface to match the sidebar. */
    [data-testid="stSidebar"] [data-baseweb="select"] > div,
    [data-testid="stSidebar"] input[type="text"],
    [data-testid="stSidebar"] textarea,
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
    [data-testid="stSidebar"] [data-testid="stTextInput"] div {{
        background: #263342 !important;
        color: #E7ECF2 !important;
        border-color: rgba(255,255,255,0.18) !important;
    }}
    [data-testid="stSidebar"] [data-baseweb="select"] svg {{
        fill: #E7ECF2 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * {{
        color: #E7ECF2 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {{
        background: {PRIMARY} !important;
        color: #fff !important;
        border: none !important;
    }}
    /* Dropdown menu that pops out of the selectbox renders in a portal
       outside the sidebar container, so it isn't dark by default. */
    div[data-baseweb="popover"] li {{
        background: #263342 !important;
        color: #E7ECF2 !important;
    }}
    div[data-baseweb="popover"] li:hover {{
        background: #34465A !important;
    }}
    /* Radio button labels in the sidebar */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {{
        color: #E7ECF2 !important;
    }}
    .stButton>button {{
        background:{PRIMARY}; color:white; border:none; border-radius:8px;
        font-weight:600; padding:0.55rem 1.1rem;
    }}
    .stButton>button:hover {{ background:#0C3D69; color:white; }}
    .stDownloadButton>button {{
        background:{ACCENT}; color:#2B2305; border:none; border-radius:8px; font-weight:700;
    }}
    .log-line {{ font-family: 'SF Mono', Consolas, monospace; font-size:0.8rem; color:{MUTED}; padding:2px 0; }}
    .derived-tag {{
        display:inline-block; background:#EEF2F7; color:{MUTED}; font-size:0.7rem;
        padding:1px 7px; border-radius:999px; margin-left:4px;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "outcomes" not in st.session_state:
    st.session_state["outcomes"] = []
if "report_path" not in st.session_state:
    st.session_state["report_path"] = None
if "query_register_path" not in st.session_state:
    st.session_state["query_register_path"] = None
if "last_run_at" not in st.session_state:
    st.session_state["last_run_at"] = None

STATUS_CHIP = {
    "PASS":    ("chip-pass",    "PASS"),
    "WARNING": ("chip-warning", "WARNING"),
    "FAIL":    ("chip-fail",    "FAIL"),
    "ERROR":   ("chip-error",   "ERROR"),
}


def status_chip(status: str) -> str:
    cls, label = STATUS_CHIP.get(status, ("chip-warning", status))
    return f'<span class="status-chip {cls}">{label}</span>'


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### ⚙️ System Status")
    st.caption(f"LLM provider: **{active_provider()}**  ·  set via `LLM_PROVIDER` env var")
    ollama_ok = check_connection()
    if settings.llm_provider == "ollama":
        if ollama_ok:
            st.success(f"Ollama connected · `{settings.ollama.host}`", icon="✅")
        else:
            st.error(f"Ollama unreachable at `{settings.ollama.host}`", icon="⚠️")
            st.caption("Run `ollama serve`, then `ollama pull llama3.2 && ollama pull qwen3-vl:8b-instruct`.")
        st.caption(
            f"Vision model: **{settings.ollama.vision_model}**  ·  "
            f"Text model: **{settings.ollama.text_model}**"
        )
        st.caption(
            f"Timeout: **{settings.ollama.request_timeout_seconds}s** per call  ·  "
            f"Keep-alive: **{settings.ollama.keep_alive}**"
        )
    elif settings.llm_provider == "gemini":
        if ollama_ok:
            st.success("Gemini API key valid", icon="✅")
        else:
            st.error("Gemini unreachable — missing/invalid key, or quota is 0", icon="⚠️")
            st.caption("Get a free key at https://aistudio.google.com/apikey and set it as an environment variable.")
        st.caption(f"Vision/text model: **{settings.gemini.vision_model}** (free tier)")
    else:
        if ollama_ok:
            st.success("Groq API key valid", icon="✅")
        else:
            st.error("Groq unreachable — missing or invalid GROQ_API_KEY", icon="⚠️")
            st.caption("Get a free key (no card) at https://console.groq.com/keys and set it as an environment variable.")
        st.caption(f"Vision model: **{settings.groq.vision_model}**  ·  Text model: **{settings.groq.text_model}** (free tier)")
    if not ollama_ok:
        pass  # error already shown above
    else:
        st.info(
            "⏱ First call per batch may take 1–3 min while the model loads. "
            "Subsequent calls are faster once it's warm.",
            icon=None,
        )

    st.markdown("---")
    st.markdown("### 📂 Intake")
    sharepoint_ok = intake.sharepoint_available()
    gmail_ok = intake.gmail_available()
    source_options = ["Upload files", "Use intake folder"]
    source_options.append("SharePoint folder" + ("" if sharepoint_ok else " (not configured)"))
    source_options.append("Gmail inbox" + ("" if gmail_ok else " (not configured)"))
    mode = st.radio("Source", source_options, label_visibility="collapsed")
    if mode.startswith("Gmail"):
        channel = "Email"
        st.caption("Submission channel: **Email** (forced for the Gmail source).")
    else:
        channel = st.selectbox(
            "Submission channel", ["Email", "Walk-in", "Unknown"],
            help="Tagged at upload per the requirements doc's metadata field. Applied to every "
                 "document in this batch.",
        )

    uploaded_files = None
    intake_dir = settings.paths.intake_dir
    if mode == "Upload files":
        uploaded_files = st.file_uploader("Drop PDF forms here", type=["pdf"], accept_multiple_files=True)
    elif mode == "Use intake folder":
        intake_dir = Path(st.text_input("Intake folder path", value=str(settings.paths.intake_dir)))
        existing = sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []
        st.caption(f"{len(existing)} PDF(s) found in folder.")
    elif mode.startswith("SharePoint"):
        if sharepoint_ok:
            st.caption(f"Watching `{settings.sharepoint.submission_folder}` on the configured SharePoint site.")
            if settings.sharepoint.write_back_enabled:
                st.caption("Write-back is ON — filed documents are also pushed to SharePoint.")
        else:
            st.caption(
                "Set SHAREPOINT_TENANT_ID / SHAREPOINT_CLIENT_ID / SHAREPOINT_CLIENT_SECRET / "
                "SHAREPOINT_SITE_ID as environment variables to enable this source."
            )
    elif mode.startswith("Gmail"):
        if gmail_ok:
            st.caption(f"Query: `{settings.gmail.query}`")
        else:
            st.caption("Set GMAIL_CREDENTIALS_FILE (OAuth client secret JSON path) to enable this source.")

    st.markdown("---")
    run_clicked = st.button("▶  Run Batch", width="stretch", disabled=not ollama_ok)
    if not ollama_ok:
        if settings.llm_provider == "ollama":
            st.caption("Start Ollama to enable processing.")
        elif settings.llm_provider == "gemini":
            st.caption("Fix the Gemini API key/quota issue above to enable processing.")
        else:
            st.caption("Fix the Groq API key issue above to enable processing.")

    with st.expander("Form types in scope"):
        for ft in load_form_types()["form_types"]:
            st.markdown(f"**{ft['code']}** — {ft['name']}")

    with st.expander("Funds & Cut-off times"):
        for fund in load_form_types().get("funds", []):
            cat_icon = "🟢" if fund["type"] == "Money Market" else "🔵"
            st.markdown(f"{cat_icon} **{fund['name']}** · Cut-off: `{fund['cutoff_time']}`")

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="bifm-header">
        <div style="font-size:1.8rem;">📄</div>
        <div>
            <h1>BIFM Unit Trusts — Document Processing</h1>
            <p>Classification · Field Extraction · Validation · Filing · Excel Report — POC</p>
        </div>
        <div class="bifm-badge">ALL 6 FORM TYPES · 100% LOCAL</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Run the batch
# --------------------------------------------------------------------------- #
def _resolve_pdf_paths() -> list[Path]:
    if mode == "Upload files":
        if not uploaded_files:
            return []
        settings.paths.intake_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in uploaded_files:
            dest = settings.paths.intake_dir / f.name
            dest.write_bytes(f.getbuffer())
            saved.append(dest)
        return saved
    if mode.startswith("SharePoint"):
        return intake.pull_from_sharepoint(progress_cb=st.write)
    if mode.startswith("Gmail"):
        return intake.pull_from_gmail(progress_cb=st.write)
    return sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []


if run_clicked:
    pdf_paths = _resolve_pdf_paths()
    if not pdf_paths:
        st.warning("No PDF files to process. Upload files or point at a non-empty intake folder.")
    else:
        progress_box = st.empty()
        log_box = st.empty()
        progress_bar = st.progress(0.0)

        # Thread-safe log queue: worker threads enqueue messages here;
        # only the main thread calls st.* functions (Streamlit context is
        # not available on ThreadPoolExecutor workers).
        import queue as _queue
        _log_q: _queue.Queue = _queue.Queue()

        def progress_cb(message: str) -> None:
            """Called from worker threads — must NOT touch Streamlit."""
            _log_q.put(message)

        def _drain_log(log_lines: list) -> None:
            """Drain the queue and redraw the log box on the main thread."""
            while not _log_q.empty():
                try:
                    log_lines.append(_log_q.get_nowait())
                except _queue.Empty:
                    break
            if log_lines:
                log_box.markdown(
                    "\n".join(f'<div class="log-line">▸ {l}</div>' for l in log_lines[-200:]),
                    unsafe_allow_html=True,
                )

        report = ExcelReportBuilder()
        query_register = QueryRegisterBuilder()
        log_lines: list[str] = []
        total = len(pdf_paths)
        progress_box.info(
            f"Processing {total} document(s) for this batch (one investor) — "
            f"up to {settings.max_workers} in parallel..."
        )

        # process_batch treats every PDF in this upload as ONE PERSON's set
        # of forms: it extracts all of them (concurrently, throttled to
        # Groq's TPM budget internally), merges identity/contact/banking
        # fields across all the documents, backfills blanks from that
        # merged profile, then validates/files/reports each one. Run it on
        # a background thread so the Streamlit main thread stays free to
        # poll the progress log (worker threads must never touch st.*).
        _batch_result: dict[str, list] = {}

        def _run_batch() -> None:
            _batch_result["outcomes"] = process_batch(
                pdf_paths, report, progress_cb, channel, query_register=query_register,
            )

        worker_thread = threading.Thread(target=_run_batch, daemon=True)
        worker_thread.start()
        while worker_thread.is_alive():
            _drain_log(log_lines)
            done = sum(1 for l in log_lines if ": complete →" in l or l.startswith("ERROR processing"))
            progress_bar.progress(min(done / total, 0.99))
            time.sleep(0.3)
        worker_thread.join()

        _drain_log(log_lines)  # final drain
        progress_bar.progress(1.0)
        outcomes: list[DocumentOutcome] = _batch_result.get("outcomes", [])
        report_path = report.save()
        query_register_path = query_register.save()
        progress_box.empty()
        progress_bar.empty()
        st.session_state["outcomes"] = outcomes
        st.session_state["report_path"] = report_path
        st.session_state["query_register_path"] = query_register_path
        st.session_state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.success(f"Batch complete — {total} document(s) processed.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Results dashboard
# --------------------------------------------------------------------------- #
outcomes: list[DocumentOutcome] = st.session_state.outcomes

if not outcomes:
    st.info("No batch has been run yet. Add PDFs in the sidebar and click **Run Batch** to begin.")
else:
    pass_n  = sum(1 for o in outcomes if o.validation_status == "PASS")
    warn_n  = sum(1 for o in outcomes if o.validation_status == "WARNING")
    fail_n  = sum(1 for o in outcomes if o.validation_status in ("FAIL", "ERROR"))
    other_n = len(outcomes) - pass_n - warn_n - fail_n

    # Summary metrics
    cols = st.columns(5)
    metrics = [
        ("Documents processed", str(len(outcomes)), INK),
        ("Passed",              str(pass_n),         PASS_C),
        ("Warnings",            str(warn_n),         WARN_C),
        ("Failed / Error",      str(fail_n),         FAIL_C),
        ("Other",               str(other_n),        MUTED),
    ]
    for col, (label, value, color) in zip(cols, metrics):
        col.markdown(
            f'<div class="metric-card"><div class="label">{label}</div>'
            f'<div class="value" style="color:{color};">{value}</div></div>',
            unsafe_allow_html=True,
        )

    st.write("")

    # Form type breakdown
    form_counts: dict[str, int] = {}
    for o in outcomes:
        form_counts[o.form_code] = form_counts.get(o.form_code, 0) + 1
    if form_counts:
        st.markdown('<div class="section-title">Form Types Processed</div>', unsafe_allow_html=True)
        breakdown_cols = st.columns(min(len(form_counts), 6))
        for col, (code, count) in zip(breakdown_cols, form_counts.items()):
            col.markdown(
                f'<div class="metric-card"><div class="label">{code}</div>'
                f'<div class="value" style="font-size:1.4rem;color:{INK};">{count}</div></div>',
                unsafe_allow_html=True,
            )
        st.write("")

    left, right = st.columns([3, 2])

    with left:
        st.markdown('<div class="section-title">Processing Results</div>', unsafe_allow_html=True)
        table_rows = [
            {
                "Document":   o.filename,
                "Form Type":  o.form_code,
                "Confidence": f"{o.classification_confidence:.0f}%",
                "Status":     o.validation_status,
                "Fund Category": (
                    o.extraction.field_value("fund_category") if o.extraction else ""
                ) or "",
                "Cut-off": (
                    o.extraction.field_value("processing_cutoff") if o.extraction else ""
                ) or "",
            }
            for o in outcomes
        ]
        df = pd.DataFrame(table_rows)

        def _style_status(val: str) -> str:
            color = {"PASS": PASS_C, "WARNING": WARN_C, "FAIL": FAIL_C, "ERROR": FAIL_C}.get(val, MUTED)
            return f"color:{color}; font-weight:700;"

        st.dataframe(
            df.style.map(_style_status, subset=["Status"]),
            width="stretch",
            hide_index=True,
        )

    with right:
        st.markdown('<div class="section-title">Download</div>', unsafe_allow_html=True)
        report_path = st.session_state.report_path
        if report_path and Path(report_path).exists():
            st.caption(f"Last run: {st.session_state.last_run_at}")
            with open(report_path, "rb") as f:
                st.download_button(
                    "⬇  Download Excel Report",
                    data=f.read(),
                    file_name=Path(report_path).name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )
            st.caption(
                "Sheets: Consolidated Investor Profile · Investor Master · Beneficiary Details · "
                "Validation Flags · Pre-Validation Flags · Confidence Scores · Processing Log"
            )

        query_register_path = st.session_state.query_register_path
        if query_register_path and Path(query_register_path).exists():
            with open(query_register_path, "rb") as f:
                st.download_button(
                    "⬇  Download Query Register",
                    data=f.read(),
                    file_name=Path(query_register_path).name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )
            st.caption("2 sheets: Query Log · Recon (matches BIFM's shared Query Register format)")

        filed_dir = settings.paths.filed_dir
        filed_count = len(list(filed_dir.glob("*"))) if filed_dir.exists() else 0
        st.caption(f"📁 {filed_count} document(s) renamed & filed to `output/filed_documents/`")

    st.write("")
    st.markdown('<div class="section-title">Document Detail</div>', unsafe_allow_html=True)

    for o in outcomes:
        chip = status_chip(o.validation_status)
        fund_cat = ""
        if o.extraction:
            fc = o.extraction.field_value("fund_category")
            if fc:
                fund_cat = f" · {fc}"

        with st.expander(
            f"{o.filename}  ·  {o.form_code}{fund_cat}  ·  {o.classification_confidence:.0f}% confidence"
        ):
            st.markdown(chip, unsafe_allow_html=True)

            if o.error:
                st.error(o.error)

            if o.extraction and o.extraction.fields:
                # Separate derived/system fields from extracted fields for clarity
                derived_ids = {
                    "form_type", "fund_category", "processing_cutoff", "instruction_mode",
                    "sub_instruction_type", "static_sub_type", "kyc_completeness_flag",
                }
                extracted_rows = [
                    {"Field": fid, "Value": str(fv.value), "Confidence": f"{fv.confidence:.0f}%", "Source": "Extracted"}
                    for fid, fv in o.extraction.fields.items()
                    if fid not in derived_ids
                ]
                derived_rows = [
                    {"Field": fid, "Value": str(fv.value), "Confidence": "—", "Source": "Derived"}
                    for fid, fv in o.extraction.fields.items()
                    if fid in derived_ids
                ]

                if extracted_rows:
                    st.caption("**Extracted Fields**")
                    st.dataframe(pd.DataFrame(extracted_rows), width="stretch", hide_index=True)

                if derived_rows:
                    st.caption("**System-Derived Metadata**")
                    st.dataframe(pd.DataFrame(derived_rows), width="stretch", hide_index=True)

            if o.extraction and o.extraction.beneficiaries:
                st.caption("**Beneficiaries**")
                st.dataframe(
                    pd.DataFrame([
                        {"Name": b.name, "Relationship": b.relationship, "Split %": b.split_percent}
                        for b in o.extraction.beneficiaries
                    ]),
                    width="stretch",
                    hide_index=True,
                )

            if o.validation and o.validation.results:
                failing = [
                    {"Field": r.field_id, "Status": r.status.value, "Message": r.message, "Value": str(r.value or "")}
                    for r in o.validation.results
                    if r.status != ValidationStatus.PASS
                ]
                st.caption("**Validation Issues**" if failing else "**Validation**")
                if failing:
                    st.dataframe(pd.DataFrame(failing), width="stretch", hide_index=True)
                else:
                    st.success("All validation checks passed.", icon="✅")
