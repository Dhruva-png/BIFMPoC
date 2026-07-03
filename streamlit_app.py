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
from app.services.consolidator import SHAREABLE_FIELDS, build_person_profile
from app.services.report_generator import ExcelReportBuilder
from app.utils.config_loader import get_fields_for_form, load_form_types
from app.utils.confidence import needs_recheck
from app.utils.field_export import fields_as_list_of_lists
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
PASS_C  = "#146039"
WARN_C  = "#835311"
FAIL_C  = "#C0392B"
BG_CARD = "#FFFFFF"
INK     = "#1B2733"
MUTED   = "#5C6B7A"

# Sidebar-only palette (dark panel sitting to the left of the light canvas).
SB_BG_TOP  = "#0B1420"
SB_BG_BOT  = "#17222F"
SB_CARD    = "#1D2A3A"
SB_BORDER  = "rgba(255,255,255,0.09)"
SB_TEXT    = "#EDF2F7"
SB_MUTED   = "#9FB0C3"
BORDER     = "#E3E8EE"

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    html, body, .stApp {{
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }}
    .stApp {{
        background:
            radial-gradient(1100px 550px at 100% -10%, rgba(15,76,129,0.07) 0%, rgba(15,76,129,0) 60%),
            linear-gradient(180deg, #F4F7FB 0%, #EEF2F7 100%);
    }}
    .block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1320px; }}

    /* ============================================================ */
    /* SIDEBAR SHELL                                                  */
    /* ============================================================ */
    [data-testid="stSidebar"] {{
        background: linear-gradient(190deg, {SB_BG_TOP} 0%, {SB_BG_BOT} 100%);
        border-right: 1px solid rgba(255,255,255,0.06);
    }}
    [data-testid="stSidebar"] * {{ color: {SB_TEXT} !important; opacity: 1 !important; }}
    [data-testid="stSidebar"] hr {{ border-color: {SB_BORDER}; margin: 1.15rem 0; }}
    [data-testid="stSidebar"] ::-webkit-scrollbar {{ width: 8px; }}
    [data-testid="stSidebar"] ::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 8px; }}
    [data-testid="stSidebar"] ::-webkit-scrollbar-track {{ background: transparent; }}

    /* Section headings, e.g. "### System Status" / "### Intake" */
    [data-testid="stSidebar"] h3 {{
        font-size: 0.86rem !important; font-weight: 800 !important; letter-spacing: .04em;
        text-transform: uppercase; color: {SB_TEXT} !important;
        padding-bottom: 9px; margin: 0 0 6px 0 !important;
        border-bottom: 1px solid {SB_BORDER};
    }}

    /* Streamlit renders st.caption text at reduced opacity by default, which
       combined with the dark sidebar background made it very low-contrast.
       Force full opacity and a legible light gray. */
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] * {{
        color: {SB_MUTED} !important; opacity: 1 !important; line-height: 1.5;
    }}
    [data-testid="stSidebar"] small {{ color: {SB_MUTED} !important; opacity: 1 !important; }}

    /* Sidebar markdown headers and radio-option labels were picking up a
       stray accent-color highlight background from Streamlit's own widget
       styling. Force them fully transparent. */
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3, [data-testid="stSidebar"] h4,
    [data-testid="stSidebar"] h5, [data-testid="stSidebar"] h6,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] *,
    [data-testid="stSidebar"] [data-testid="stHeadingWithActionElements"],
    [data-testid="stSidebar"] [role="radiogroup"],
    [data-testid="stSidebar"] [role="radiogroup"] * {{
        background: transparent !important;
        background-color: transparent !important;
        box-shadow: none !important;
    }}
    [data-testid="stSidebar"] [role="radiogroup"] label {{
        background: transparent !important; padding: 2px 0;
    }}
    [data-testid="stSidebar"] [data-testid="stRadio"] {{
        background: {SB_CARD}; border: 1px solid {SB_BORDER};
        border-radius: 10px; padding: 10px 12px 4px 12px;
    }}

    /* ---- st.success / st.info / st.warning / st.error in the sidebar ---- */
    [data-testid="stSidebar"] [data-testid="stAlertContainer"] {{
        border-radius: 10px !important;
        background: {SB_CARD} !important;
        border: 1px solid {SB_BORDER} !important;
        padding: 0.75rem 0.95rem !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {{
        border-left: 3px solid #2FBF71 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {{
        border-left: 3px solid #4E9BFF !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {{
        border-left: 3px solid {ACCENT} !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {{
        border-left: 3px solid #F0685C !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlertContentSuccess"],
    [data-testid="stSidebar"] [data-testid="stAlertContentSuccess"] *,
    [data-testid="stSidebar"] [data-testid="stAlertContentInfo"],
    [data-testid="stSidebar"] [data-testid="stAlertContentInfo"] *,
    [data-testid="stSidebar"] [data-testid="stAlertContentWarning"],
    [data-testid="stSidebar"] [data-testid="stAlertContentWarning"] *,
    [data-testid="stSidebar"] [data-testid="stAlertContentError"],
    [data-testid="stSidebar"] [data-testid="stAlertContentError"] * {{
        color: {SB_TEXT} !important; opacity: 1 !important;
    }}

    /* File uploader helper text ("Drag and drop..." / "200MB per file...") */
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"],
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] * {{
        color: #DCE6F0 !important; opacity: 1 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {{
        background: {SB_CARD} !important;
        border: 1.5px dashed rgba(255,255,255,0.22) !important;
        border-radius: 12px !important;
        transition: border-color .15s ease;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]:hover {{
        border-color: {ACCENT} !important;
    }}

    /* Selectbox / text input controls sitting on the dark sidebar */
    [data-testid="stSidebar"] [data-baseweb="select"] > div,
    [data-testid="stSidebar"] [data-testid="stTextInput"] input {{
        background: {SB_CARD} !important;
        border: 1px solid {SB_BORDER} !important;
        border-radius: 8px !important;
    }}
    /* Selectbox: closed control sits on the dark sidebar, so its text needs
       to be light. The open dropdown menu is rendered by Streamlit in a
       white popover, so its option text needs to stay dark - regardless of
       our sidebar-wide light-text rule (the popover isn't a descendant of
       stSidebar in the DOM, but we pin it explicitly to be safe). */
    [data-testid="stSidebar"] [data-baseweb="select"] * {{
        color: #FFFFFF !important;
    }}
    [data-testid="stSidebar"] [role="radiogroup"] label p,
    [data-testid="stSidebar"] [role="radiogroup"] label span,
    [data-testid="stSidebar"] [data-testid="stTextInput"] input,
    [data-testid="stSidebar"] [data-testid="stTextInput"] label p {{
        color: #FFFFFF !important;
    }}
    [data-baseweb="popover"] [data-baseweb="menu"],
    [data-baseweb="popover"] ul[role="listbox"] {{
        background: #FFFFFF !important;
    }}
    [data-baseweb="popover"] [role="option"],
    [data-baseweb="popover"] [role="option"] * {{
        color: {INK} !important;
        background: #FFFFFF !important;
    }}
    [data-baseweb="popover"] [role="option"]:hover,
    [data-baseweb="popover"] [role="option"][aria-selected="true"] {{
        background: #EEF2F7 !important;
    }}

    /* Sidebar expanders: dark card, light text */
    [data-testid="stSidebar"] div[data-testid="stExpander"] {{
        background: {SB_CARD} !important;
        border: 1px solid {SB_BORDER} !important;
        border-radius: 10px !important;
        overflow: hidden;
    }}
    [data-testid="stSidebar"] div[data-testid="stExpander"] * {{ color: {SB_TEXT} !important; }}
    [data-testid="stSidebar"] details summary {{ padding: 4px 2px; transition: background .15s ease; }}
    [data-testid="stSidebar"] details summary:hover {{ background: rgba(255,255,255,0.04); }}
    [data-testid="stSidebar"] details summary p {{ color: {SB_TEXT} !important; font-weight: 600; }}

    /* File uploader's own "Browse files" button uses Streamlit's
       stBaseButton-secondary component - scoped to stFileUploader so it
       doesn't collide with st.button (also "secondary" by default, see
       below). */
    [data-testid="stSidebar"] [data-testid="stFileUploader"] [data-testid="stBaseButton-secondary"] {{
        background: #2B3B4F !important;
        border: 1px solid rgba(255,255,255,0.25) !important;
        border-radius: 7px !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploader"] [data-testid="stBaseButton-secondary"] * {{
        color: {SB_TEXT} !important;
    }}
    /* st.button() defaults to type="secondary" unless type="primary" is
       passed, so both share the stBaseButton-secondary testid. Scope to
       stButton so "Run Batch" gets the intended primary-blue treatment
       regardless of which `type` was used. */
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-secondary"],
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-primary"] {{
        background: linear-gradient(135deg, {PRIMARY} 0%, #16649C 100%) !important;
        border: none !important; border-radius: 9px !important;
        box-shadow: 0 4px 14px rgba(15,76,129,0.35);
        transition: transform .12s ease, box-shadow .12s ease;
        font-weight: 700 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-secondary"]:hover,
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-primary"]:hover {{
        transform: translateY(-1px); box-shadow: 0 6px 18px rgba(15,76,129,0.45);
    }}
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-secondary"] *,
    [data-testid="stSidebar"] [data-testid="stButton"] [data-testid="stBaseButton-primary"] * {{
        color: #FFFFFF !important;
    }}
    [data-testid="stSidebar"] [data-testid="stButton"] button:disabled {{
        background: #29384A !important; box-shadow: none !important; opacity: .5 !important;
    }}

    /* ============================================================ */
    /* HEADER BANNER                                                  */
    /* ============================================================ */
    .bifm-header {{
        position: relative; overflow: hidden;
        display:flex; align-items:center; gap:16px;
        padding: 22px 28px; margin-bottom: 18px;
        background: linear-gradient(120deg, {PRIMARY} 0%, #0B3057 100%);
        border-radius: 16px; color: white;
        box-shadow: 0 10px 26px rgba(15,76,129,0.28);
    }}
    .bifm-header::after {{
        content: ""; position: absolute; inset: 0;
        background: radial-gradient(420px 200px at 92% 0%, rgba(201,162,75,0.22) 0%, rgba(201,162,75,0) 70%);
        pointer-events: none;
    }}
    .bifm-header .bifm-icon {{
        display:flex; align-items:center; justify-content:center;
        width: 46px; height: 46px; border-radius: 12px; font-size: 1.5rem; flex-shrink: 0;
        background: rgba(255,255,255,0.14); border: 1px solid rgba(255,255,255,0.22);
    }}
    .bifm-header h1 {{ font-size: 1.5rem; margin:0; font-weight:800; letter-spacing:.2px; }}
    .bifm-header p {{ margin:3px 0 0 0; color:#CFE0F2; font-size:0.86rem; }}
    .bifm-badge {{
        background: rgba(255,255,255,0.14); color:#fff; font-size:0.72rem; font-weight:700;
        padding: 5px 12px; border-radius:999px; border:1px solid rgba(255,255,255,0.28);
        margin-left:auto; white-space:nowrap; letter-spacing:.03em; z-index: 1;
    }}

    /* ============================================================ */
    /* METRIC CARDS                                                   */
    /* ============================================================ */
    .metric-card {{
        position: relative; overflow: hidden;
        background:{BG_CARD}; border-radius:14px; padding:16px 18px 16px 20px;
        border:1px solid {BORDER}; box-shadow:0 2px 10px rgba(15,23,42,0.05);
        transition: transform .12s ease, box-shadow .12s ease;
    }}
    .metric-card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 20px rgba(15,23,42,0.09); }}
    .metric-card::before {{
        content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
        background: currentColor; opacity:.6;
    }}
    .metric-card .label {{ color:{MUTED}; font-size:0.76rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }}
    .metric-card .value {{ color:{INK}; font-size:1.9rem; font-weight:800; line-height:1.25; margin-top:2px; }}

    /* ============================================================ */
    /* STATUS CHIPS                                                   */
    /* ============================================================ */
    .status-chip {{
        display:inline-flex; align-items:center; gap:6px;
        padding:4px 12px; border-radius:999px;
        font-size:0.72rem; font-weight:800; letter-spacing:.03em;
    }}
    .status-chip::before {{ content:""; width:6px; height:6px; border-radius:50%; background:currentColor; flex-shrink:0; }}
    .chip-pass    {{ background:#E4F6EC; color:{PASS_C}; }}
    .chip-warning {{ background:#FCF1DD; color:{WARN_C}; }}
    .chip-fail    {{ background:#FBE6E4; color:{FAIL_C}; }}
    .chip-error   {{ background:#FBE6E4; color:{FAIL_C}; }}

    /* ============================================================ */
    /* SECTION TITLES & CONTENT CARDS                                 */
    /* ============================================================ */
    .section-title {{
        font-size:1.02rem; font-weight:800; color:{INK}; margin: 22px 0 12px 0;
        padding-left: 12px; border-left: 4px solid {PRIMARY};
        display:flex; align-items:center; gap:8px;
    }}
    div[data-testid="stExpander"] {{
        background:{BG_CARD}; border-radius:12px; border:1px solid {BORDER};
        box-shadow: 0 1px 6px rgba(15,23,42,0.04); overflow: hidden;
    }}
    div[data-testid="stExpander"] summary {{ padding: 4px 2px; transition: background .15s ease; }}
    div[data-testid="stExpander"] summary:hover {{ background: #F7F9FC; }}

    .stButton>button,
    [data-testid="stButton"] [data-testid="stBaseButton-primary"],
    [data-testid="stButton"] [data-testid="stBaseButton-secondary"] {{
        background: linear-gradient(135deg, {PRIMARY} 0%, #16649C 100%) !important;
        color:white !important; border:none !important; border-radius:9px;
        font-weight:700; padding:0.6rem 1.2rem;
        box-shadow: 0 4px 14px rgba(15,76,129,0.25);
        transition: transform .12s ease, box-shadow .12s ease;
    }}
    .stButton>button:hover {{ transform: translateY(-1px); box-shadow: 0 6px 18px rgba(15,76,129,0.35); }}
    .stButton>button *,
    [data-testid="stButton"] [data-testid="stBaseButton-primary"] *,
    [data-testid="stButton"] [data-testid="stBaseButton-secondary"] * {{ color: white !important; }}
    .stDownloadButton>button,
    [data-testid="stDownloadButton"] [data-testid="stBaseButton-primary"],
    [data-testid="stDownloadButton"] [data-testid="stBaseButton-secondary"] {{
        background: linear-gradient(135deg, {ACCENT} 0%, #B58B32 100%) !important;
        color:#2B2305 !important; border:none; border-radius:9px; font-weight:800;
        box-shadow: 0 4px 14px rgba(201,162,75,0.3);
        transition: transform .12s ease, box-shadow .12s ease;
    }}
    .stDownloadButton>button:hover {{ transform: translateY(-1px); box-shadow: 0 6px 18px rgba(201,162,75,0.4); }}
    [data-testid="stDownloadButton"] [data-testid="stBaseButton-primary"] *,
    [data-testid="stDownloadButton"] [data-testid="stBaseButton-secondary"] * {{ color:#2B2305 !important; }}

    .log-console {{
        background: #101823; border: 1px solid #1F2C3B; border-radius: 12px;
        padding: 14px 16px; max-height: 320px; overflow-y: auto;
    }}
    .log-line {{ font-family: 'SF Mono', Consolas, monospace; font-size:0.8rem; color:#9FB0C3; padding:2px 0; }}
    .derived-tag {{
        display:inline-block; background:#EEF2F7; color:{MUTED}; font-size:0.7rem; font-weight:600;
        padding:2px 9px; border-radius:999px; margin-left:6px; vertical-align: middle;
    }}
    .kv-item {{
        background:#F7F9FC; border:1px solid {BORDER}; border-radius:10px;
        padding:10px 13px; margin-bottom:10px; transition: border-color .15s ease;
    }}
    .kv-item:hover {{ border-color: #C7D3E0; }}
    .kv-label {{
        color:{MUTED}; font-size:0.67rem; font-weight:700; text-transform:uppercase;
        letter-spacing:.04em; margin-bottom:3px;
    }}
    .kv-value {{ color:{INK}; font-size:0.93rem; font-weight:600; word-break:break-word; }}
    .profile-card {{
        background:{BG_CARD}; border-radius:14px; padding:20px 22px 8px 22px;
        border:1px solid {BORDER}; box-shadow:0 2px 10px rgba(15,23,42,0.05); margin-bottom:16px;
    }}

    /* Our cards/expanders always sit on a light background (BG_CARD or
       similar), but plain Streamlit text (captions, markdown paragraphs,
       list items, code blocks) otherwise inherits whatever text color the
       viewer's Streamlit theme uses - which is near-white in dark mode,
       making it unreadable against our light backgrounds. Force a dark,
       readable color on those plain elements specifically. Elements that
       already set their own explicit color (status chips, kv-item
       label/value, derived-tag, metric-card label/value) are excluded via
       :not() so their pass/warn/fail color-coding is untouched. */
    .stApp div[data-testid="stExpander"] p,
    .stApp div[data-testid="stExpander"] li,
    .stApp div[data-testid="stExpander"] label,
    .stApp div[data-testid="stExpander"] span:not(.status-chip),
    .stApp div[data-testid="stExpander"] summary,
    .stApp [data-testid="stCaptionContainer"],
    .stApp [data-testid="stMarkdownContainer"] p,
    .metric-card p, .profile-card p, .profile-card li {{
        color: {INK} !important;
    }}

    /* st.success / st.info / st.warning / st.error in the main (light)
       content area - a consistent card treatment with a colored left
       accent instead of Streamlit's flat default look. */
    .stApp [data-testid="stAlertContainer"] {{
        border-radius: 10px !important; border: 1px solid {BORDER} !important;
    }}
    .stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {{
        background:#EFFBF4 !important; border-left: 3px solid {PASS_C} !important;
    }}
    .stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {{
        background:#EEF5FF !important; border-left: 3px solid #2563EB !important;
    }}
    .stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {{
        background:#FFF8EC !important; border-left: 3px solid {WARN_C} !important;
    }}
    .stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {{
        background:#FDEEEC !important; border-left: 3px solid {FAIL_C} !important;
    }}

    /* st.code / st.json blocks: force a readable light-on-dark pairing
       explicitly rather than relying on the ambient theme. */
    .stApp [data-testid="stCodeBlock"] pre,
    .stApp [data-testid="stCodeBlock"] code {{
        background: #101823 !important; color: #E7ECF2 !important; border-radius: 10px !important;
    }}

    /* Dataframes get the same card treatment as everything else. */
    .stApp [data-testid="stDataFrame"] {{
        border: 1px solid {BORDER}; border-radius: 12px; overflow: hidden;
        box-shadow: 0 1px 6px rgba(15,23,42,0.04);
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


# Fields shown once in the batch-level Investor Profile card rather than
# repeated inside every single document's card below (name, ID, contact,
# banking details are the same person's details regardless of which of
# their 4 forms you're looking at).
_IDENTITY_FIELD_IDS = set(SHAREABLE_FIELDS)

# A handful of core identity fields worth flagging explicitly if NO document
# in the batch captured them at all (nothing to backfill from).
_CORE_IDENTITY_CHECK = ("full_name", "entity_number", "id_number", "contact_number", "email")


def _pretty_label(field_id: str, form_code: str | None = None) -> str:
    """Human-friendly label for a field id, preferring the form's own config label."""
    if form_code:
        for f in get_fields_for_form(form_code):
            if f["id"] == field_id:
                return f["label"]
    return field_id.replace("_", " ").title()


def _render_kv_grid(items: list[tuple[str, str]], n_cols: int = 3) -> None:
    """Renders a compact grid of label/value chips - the 'concatenated summary' view."""
    if not items:
        return
    cols = st.columns(n_cols)
    for i, (label, value) in enumerate(items):
        with cols[i % n_cols]:
            st.markdown(
                f'<div class="kv-item"><div class="kv-label">{label}</div>'
                f'<div class="kv-value">{value}</div></div>',
                unsafe_allow_html=True,
            )


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
            st.success("Marvel AI running", icon="✅")
        else:
            st.error("Marvel AI unreachable — missing or invalid GROQ_API_KEY", icon="⚠️")
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
    mode = st.radio("Source", ["Upload files", "Use intake folder"], label_visibility="collapsed")
    channel = st.selectbox(
        "Submission channel", ["Email", "Walk-in", "Unknown"],
        help="Tagged at upload per the requirements doc's metadata field. Applied to every "
             "document in this batch.",
    )

    uploaded_files = None
    intake_dir = settings.paths.intake_dir
    if mode == "Upload files":
        uploaded_files = st.file_uploader("Drop PDF forms here", type=["pdf"], accept_multiple_files=True)
    else:
        intake_dir = Path(st.text_input("Intake folder path", value=str(settings.paths.intake_dir)))
        existing = sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []
        st.caption(f"{len(existing)} PDF(s) found in folder.")

    st.markdown("---")
    run_clicked = st.button("▶  Run Batch", width="stretch", disabled=not ollama_ok)
    if not ollama_ok:
        if settings.llm_provider == "ollama":
            st.caption("Start Ollama to enable processing.")
        elif settings.llm_provider == "gemini":
            st.caption("Fix the Gemini API key/quota issue above to enable processing.")
        else:
            st.caption("Fix the Marvel AI connection issue above to enable processing.")

    with st.expander("Form types in scope"):
        for ft in load_form_types()["form_types"]:
            st.markdown(f"**{ft['code']}** — {ft['name']}")

    with st.expander("Funds & Cut-off times"):
        for fund in load_form_types().get("funds", []):
            cat_icon = "🟢" if fund["type"] == "Money Market" else "🔵"
            st.markdown(f"{cat_icon} **{fund['name']}** · Cut-off: `{fund['cutoff_time']}`")

# --------------------------------------------------------------------------- #
# Logo (white card)
# --------------------------------------------------------------------------- #
_logo_path = Path(__file__).parent / "app" / "ui" / "assets" / "kgisl_marvel_logo.png"
if _logo_path.exists():
    st.markdown(
        """
        <div style="background:#FFFFFF; border-radius:14px; padding:14px 20px;
                    margin-bottom:10px; border:1px solid #E3E8EE;
                    box-shadow:0 2px 8px rgba(15,23,42,0.04);
                    display:flex; align-items:center;">
        """,
        unsafe_allow_html=True,
    )
    st.image(str(_logo_path), width=220)
    st.markdown("</div>", unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="bifm-header">
        <div class="bifm-icon">📄</div>
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
                lines_html = "\n".join(f'<div class="log-line">▸ {l}</div>' for l in log_lines[-200:])
                log_box.markdown(
                    f'<div class="log-console">{lines_html}</div>',
                    unsafe_allow_html=True,
                )

        report = ExcelReportBuilder()
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
            _batch_result["outcomes"] = process_batch(pdf_paths, report, progress_cb, channel)

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
        progress_box.empty()
        progress_bar.empty()
        st.session_state["outcomes"] = outcomes
        st.session_state["report_path"] = report_path
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
                "Confidence": (
                    f"{o.classification_confidence:.0f}%"
                    + (" ⚠" if needs_recheck(o.classification_confidence) else "")
                ),
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
            st.caption("4 sheets: Investor Master · Beneficiary Details · Validation Flags · Processing Log")

        filed_dir = settings.paths.filed_dir
        filed_count = len(list(filed_dir.glob("*"))) if filed_dir.exists() else 0
        st.caption(f"📁 {filed_count} document(s) renamed & filed to `output/filed_documents/`")

    st.write("")

    # ----------------------------------------------------------------- #
    # Investor Profile — identity/contact/banking details captured ONCE
    # across the whole batch, shown here a single time instead of being
    # repeated (and re-validated as "missing") inside every document card.
    # ----------------------------------------------------------------- #
    all_extractions = [o.extraction for o in outcomes if o.extraction]
    profile = build_person_profile(all_extractions) if all_extractions else {}

    st.markdown(
        '<div class="section-title">Investor Profile '
        '<span class="derived-tag">consolidated across this batch</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="profile-card">', unsafe_allow_html=True)

    profile_items = [
        (_pretty_label(fid), str(fv.value))
        for fid in SHAREABLE_FIELDS
        if (fv := profile.get(fid)) and fv.value not in (None, "")
    ]
    if profile_items:
        _render_kv_grid(profile_items, n_cols=4)
    else:
        st.caption("No shared identity/contact/banking details were captured across this batch.")

    missing_core = [fid for fid in _CORE_IDENTITY_CHECK if not (profile.get(fid) and profile[fid].value)]
    if missing_core:
        st.warning(
            "Not captured on **any** document in this batch: "
            + ", ".join(_pretty_label(fid) for fid in missing_core)
            + ". This needs to be sourced manually rather than backfilled.",
            icon="⚠️",
        )
    st.markdown("</div>", unsafe_allow_html=True)

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
            + (" ⚠ recheck recommended" if needs_recheck(o.classification_confidence) else "")
        ):
            st.markdown(chip, unsafe_allow_html=True)

            if o.error:
                st.error(o.error)
                continue

            # ---- Concatenated summary: every field worth this document's
            # own card (fund/instruction/amount/derived details), skipping
            # identity/contact/banking fields already shown once above so
            # the same name/ID/DOB don't get repeated on every document.
            derived_ids = {
                "form_type", "fund_category", "processing_cutoff", "fund_category_priority",
                "instruction_mode", "sub_instruction_type", "static_sub_type",
                "kyc_completeness_flag",
            }
            if o.extraction:
                st.caption("**Summary**")
                summary_items = [
                    (_pretty_label(fid, o.form_code), str(fv.value))
                    for fid, fv in o.extraction.fields.items()
                    if fid not in _IDENTITY_FIELD_IDS and fv.value not in (None, "")
                ]
                if summary_items:
                    _render_kv_grid(summary_items, n_cols=3)
                else:
                    st.caption("No instruction-specific fields were captured on this document.")

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

            # ---- Plain-language status: say what's actually still needed
            # rather than a bare FAIL. Identity/contact/banking gaps are
            # already surfaced once in the Investor Profile card above, so
            # they're excluded here to avoid repeating the same warning on
            # every single document - only this document's OWN
            # instruction-specific issues are called out.
            if o.validation and o.validation.results:
                doc_fails = [
                    r for r in o.validation.results
                    if r.status == ValidationStatus.FAIL and r.field_id not in _IDENTITY_FIELD_IDS
                ]
                doc_warnings = [
                    r for r in o.validation.results
                    if r.status == ValidationStatus.WARNING and r.field_id not in _IDENTITY_FIELD_IDS
                ]
                identity_only_fail = (
                    o.validation.overall_status == ValidationStatus.FAIL and not doc_fails
                )

                if not doc_fails and not doc_warnings:
                    if identity_only_fail:
                        st.info(
                            "All of this document's own fields are captured — the only gap is "
                            "investor identity, flagged once in the Investor Profile card above.",
                            icon="ℹ️",
                        )
                    else:
                        st.success("All required information captured and validated.", icon="✅")
                else:
                    if doc_fails:
                        st.error(
                            "Still needed on this document: "
                            + ", ".join(_pretty_label(r.field_id, o.form_code) for r in doc_fails),
                            icon="⚠️",
                        )
                    if doc_warnings:
                        st.warning(
                            "Worth a second look: "
                            + ", ".join(_pretty_label(r.field_id, o.form_code) for r in doc_warnings),
                            icon="👀",
                        )

            # ---- Raw technical detail, tucked away for power users/debugging
            with st.expander("Show raw extracted fields & validation checks", expanded=False):
                if o.extraction and o.extraction.fields:
                    extracted_rows = [
                        {
                            "Field": fid,
                            "Value": str(fv.value),
                            "Confidence": f"{fv.confidence:.0f}%" + (" ⚠" if needs_recheck(fv.confidence) else ""),
                            "Source": "Extracted",
                        }
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

                if o.validation and o.validation.results:
                    all_checks = [
                        {"Field": r.field_id, "Status": r.status.value, "Message": r.message, "Value": str(r.value or "")}
                        for r in o.validation.results
                    ]
                    st.caption("**All Validation Checks**")
                    st.dataframe(pd.DataFrame(all_checks), width="stretch", hide_index=True)

            # ---- Plain [['field', value], ...] export, per the requested
            # format. Built from EVERY field on this document's extraction
            # (both directly-extracted and system-derived), and stashed in
            # st.session_state so it's a real Python variable available for
            # the rest of the session, not just printed once and discarded.
            field_list = fields_as_list_of_lists(o.extraction) if o.extraction else []
            st.session_state.setdefault("field_lists", {})[o.filename] = field_list
            with st.expander("Show as [['field', value], ...] list", expanded=False):
                st.code(repr(field_list), language="python")
                st.caption(
                    f"Also available in `st.session_state['field_lists'][{o.filename!r}]` "
                    "for the rest of this session."
                )
