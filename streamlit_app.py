"""
BIFM Unit Trusts — Document Processing (POC)
Streamlit front end.

Run with:
    streamlit run streamlit_app.py

This is a thin presentation layer only — all business logic (classification,
extraction, validation, Excel generation, filing) still lives in app/ and is
untouched. The UI calls app.core.pipeline.process_single_document() per file
so progress can be streamed to the page as each document completes.
"""
from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core.pipeline import DocumentOutcome, process_single_document
from app.llm.ollama_client import check_connection
from app.models.schemas import ValidationStatus
from app.services.report_generator import ExcelReportBuilder
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

PRIMARY = "#0F4C81"      # deep institutional blue
ACCENT = "#C9A24B"       # restrained gold accent
PASS_C = "#1E8E5A"
WARN_C = "#B7791F"
FAIL_C = "#C0392B"
BG_CARD = "#FFFFFF"
INK = "#1B2733"
MUTED = "#5C6B7A"

st.markdown(
    f"""
    <style>
    .stApp {{
        background: linear-gradient(180deg, #F4F7FB 0%, #EEF2F7 100%);
    }}
    [data-testid="stSidebar"] {{
        background: {INK};
    }}
    [data-testid="stSidebar"] * {{ color: #E7ECF2 !important; }}
    [data-testid="stSidebar"] hr {{ border-color: rgba(255,255,255,0.15); }}

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
    .chip-pass {{ background:#E4F6EC; color:{PASS_C}; }}
    .chip-warning {{ background:#FCF1DD; color:{WARN_C}; }}
    .chip-fail {{ background:#FBE6E4; color:{FAIL_C}; }}
    .chip-not_extracted {{ background:#EAEDF1; color:{MUTED}; }}
    .chip-error {{ background:#FBE6E4; color:{FAIL_C}; }}

    .section-title {{
        font-size:1.02rem; font-weight:700; color:{INK}; margin: 6px 0 10px 0;
    }}
    div[data-testid="stExpander"] {{
        background:{BG_CARD}; border-radius:12px; border:1px solid #E3E8EE;
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
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "outcomes" not in st.session_state:
    st.session_state.outcomes: list[DocumentOutcome] = []
if "report_path" not in st.session_state:
    st.session_state.report_path: Path | None = None
if "last_run_at" not in st.session_state:
    st.session_state.last_run_at: str | None = None

STATUS_CHIP = {
    "PASS": ("chip-pass", "PASS"),
    "WARNING": ("chip-warning", "WARNING"),
    "FAIL": ("chip-fail", "FAIL"),
    "NOT_EXTRACTED": ("chip-not_extracted", "CLASSIFIED ONLY"),
    "ERROR": ("chip-error", "ERROR"),
}


def status_chip(status: str) -> str:
    cls, label = STATUS_CHIP.get(status, ("chip-not_extracted", status))
    return f'<span class="status-chip {cls}">{label}</span>'


# --------------------------------------------------------------------------- #
# Sidebar — connection status, intake source, run controls
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### ⚙️ System Status")
    ollama_ok = check_connection()
    if ollama_ok:
        st.success(f"Ollama connected · `{settings.ollama.host}`", icon="✅")
    else:
        st.error(f"Ollama unreachable at `{settings.ollama.host}`", icon="⚠️")
        st.caption("Run `ollama serve`, then `ollama pull llama3.2 && ollama pull llava`.")

    st.caption(f"Vision model: **{settings.ollama.vision_model}**  ·  Text model: **{settings.ollama.text_model}**")

    st.markdown("---")
    st.markdown("### 📂 Intake")
    mode = st.radio("Source", ["Upload files", "Use intake folder"], label_visibility="collapsed")

    uploaded_files = None
    intake_dir = settings.paths.intake_dir
    if mode == "Upload files":
        uploaded_files = st.file_uploader("Drop PDF forms here", type=["pdf"], accept_multiple_files=True)
    else:
        intake_dir = Path(st.text_input("Intake folder path", value=str(settings.paths.intake_dir)))
        existing = sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []
        st.caption(f"{len(existing)} PDF(s) found in folder.")

    st.markdown("---")
    run_clicked = st.button("▶  Run Batch", use_container_width=True, disabled=not ollama_ok)
    if not ollama_ok:
        st.caption("Start Ollama to enable processing.")

    with st.expander("Form types in scope"):
        for ft in load_form_types()["form_types"]:
            st.caption(f"**{ft['code']}** — {ft['name']}")

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="bifm-header">
        <div style="font-size:1.8rem;">📄</div>
        <div>
            <h1>BIFM Unit Trusts — Document Processing</h1>
            <p>Local, offline form classification · extraction · validation · filing — POC</p>
        </div>
        <div class="bifm-badge">100% LOCAL · NO CLOUD CALLS</div>
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
    return sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []


if run_clicked:
    pdf_paths = _resolve_pdf_paths()
    if not pdf_paths:
        st.warning("No PDF files to process. Upload files or point at a non-empty intake folder.")
    else:
        progress_box = st.empty()
        log_box = st.container(height=180)
        progress_bar = st.progress(0.0)
        log_lines: list[str] = []

        def progress_cb(message: str) -> None:
            log_lines.append(message)
            with log_box:
                st.markdown(
                    "\n".join(f'<div class="log-line">▸ {l}</div>' for l in log_lines[-200:]),
                    unsafe_allow_html=True,
                )

        report = ExcelReportBuilder()
        outcomes: list[DocumentOutcome] = []
        total = len(pdf_paths)
        progress_box.info(f"Processing {total} document(s) — up to {settings.max_workers} in parallel...")

        with ThreadPoolExecutor(max_workers=max(1, settings.max_workers)) as pool:
            futures = {pool.submit(process_single_document, p, report, progress_cb): p for p in pdf_paths}
            done = 0
            for future in as_completed(futures):
                outcomes.append(future.result())
                done += 1
                progress_bar.progress(done / total)

        report_path = report.save()
        progress_box.empty()
        progress_bar.empty()
        st.session_state.outcomes = outcomes
        st.session_state.report_path = report_path
        st.session_state.last_run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.success(f"Batch complete — {total} document(s) processed.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Results dashboard
# --------------------------------------------------------------------------- #
outcomes: list[DocumentOutcome] = st.session_state.outcomes

if not outcomes:
    st.info("No batch has been run yet. Add PDFs in the sidebar and click **Run Batch** to begin.")
else:
    pass_n = sum(1 for o in outcomes if o.validation_status == "PASS")
    warn_n = sum(1 for o in outcomes if o.validation_status == "WARNING")
    fail_n = sum(1 for o in outcomes if o.validation_status in ("FAIL", "ERROR"))
    other_n = len(outcomes) - pass_n - warn_n - fail_n

    cols = st.columns(5)
    metrics = [
        ("Documents processed", str(len(outcomes)), INK),
        ("Passed", str(pass_n), PASS_C),
        ("Warnings", str(warn_n), WARN_C),
        ("Failed", str(fail_n), FAIL_C),
        ("Classified only", str(other_n), MUTED),
    ]
    for col, (label, value, color) in zip(cols, metrics):
        col.markdown(
            f'<div class="metric-card"><div class="label">{label}</div>'
            f'<div class="value" style="color:{color};">{value}</div></div>',
            unsafe_allow_html=True,
        )

    st.write("")
    left, right = st.columns([3, 2])

    with left:
        st.markdown('<div class="section-title">Processing Results</div>', unsafe_allow_html=True)
        table_rows = [
            {
                "Document": o.filename,
                "Form Type": o.form_code,
                "Confidence": f"{o.classification_confidence:.0f}%",
                "Status": o.validation_status,
            }
            for o in outcomes
        ]
        df = pd.DataFrame(table_rows)

        def _style_status(val: str) -> str:
            color = {"PASS": PASS_C, "WARNING": WARN_C, "FAIL": FAIL_C, "ERROR": FAIL_C}.get(val, MUTED)
            return f"color:{color}; font-weight:700;"

        st.dataframe(
            df.style.map(_style_status, subset=["Status"]),
            use_container_width=True,
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
                    use_container_width=True,
                )
            st.caption(f"4 sheets: Investor Master · Beneficiary Details · Validation Flags · Processing Log")
        filed_dir = settings.paths.filed_dir
        filed_count = len(list(filed_dir.glob("*"))) if filed_dir.exists() else 0
        st.caption(f"📁 {filed_count} document(s) renamed & filed to `output/filed_documents/`")

    st.write("")
    st.markdown('<div class="section-title">Document Detail</div>', unsafe_allow_html=True)
    for o in outcomes:
        chip = status_chip(o.validation_status)
        with st.expander(f"{o.filename}  ·  {o.form_code}  ·  {o.classification_confidence:.0f}% confidence"):
            st.markdown(chip, unsafe_allow_html=True)
            if o.error:
                st.error(o.error)
            if o.extraction and o.extraction.fields:
                field_rows = [
                    {"Field": fid, "Value": str(fv.value), "Confidence": f"{fv.confidence:.0f}%"}
                    for fid, fv in o.extraction.fields.items()
                ]
                st.dataframe(pd.DataFrame(field_rows), use_container_width=True, hide_index=True)
            if o.extraction and o.extraction.beneficiaries:
                st.caption("Beneficiaries")
                st.dataframe(
                    pd.DataFrame(
                        [{"Name": b.name, "Relationship": b.relationship, "Split %": b.split_percent}
                         for b in o.extraction.beneficiaries]
                    ),
                    use_container_width=True, hide_index=True,
                )
            if o.validation and o.validation.results:
                st.caption("Validation flags")
                vrows = [
                    {"Field": r.field_id, "Status": r.status.value, "Message": r.message}
                    for r in o.validation.results if r.status != ValidationStatus.PASS
                ]
                if vrows:
                    st.dataframe(pd.DataFrame(vrows), use_container_width=True, hide_index=True)
                else:
                    st.caption("All checks passed.")
