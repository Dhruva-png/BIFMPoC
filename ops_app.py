"""
Streamlit UI for Use Case 2 - BIFM Ops: Withdrawal & Contribution Audit
Document Automation.

    streamlit run ops_app.py

Visually matched to Use Case 1's streamlit_app.py (same palette, header,
sidebar, metric cards, status chips, log styling) so the two apps read as
one platform rather than two unrelated tools - the proposal's own framing
("Both use cases run on one Marvel.ai instance").

Two tabs:
  Process - drop PDFs / exported .eml emails (or point at a folder /
            the configured IMAP mailbox), run the batch, review
            transactions, pack completeness, and exceptions.
  Search  - the metadata repository: free-text or field-scoped search
            across every extracted metadata field, spanning all runs.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import intake as uc1_intake  # noqa: E402 - reuse imap_available()
from config.settings import settings  # noqa: E402
from ops.models import METADATA_FIELDS, AuditPack, OpsDocument  # noqa: E402
from ops.ops_config import (  # noqa: E402
    OPS_INTAKE_DIR,
    ensure_output_dirs,
    load_audit_pack_definitions,
    load_document_types,
    load_salesperson_roster,
)

_ALL_SALESPEOPLE = "(all salespeople)"

# --------------------------------------------------------------------------- #
# Page config & theme - same palette and CSS as Use Case 1 (streamlit_app.py)
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="BIFM Ops — Audit Document Automation",
    page_icon="🗂️",
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

    [data-testid="stSidebar"] [data-testid="stAlert"],
    [data-testid="stSidebar"] [data-testid="stAlertContainer"],
    [data-testid="stSidebar"] .stAlert {{
        background: #EAF1F8 !important;
        color: {INK} !important;
    }}
    [data-testid="stSidebar"] [data-testid="stAlert"] *,
    [data-testid="stSidebar"] [data-testid="stAlertContainer"] *,
    [data-testid="stSidebar"] .stAlert * {{
        color: {INK} !important;
    }}

    [data-testid="stSidebar"] code {{
        background: #34465A !important;
        color: #E7ECF2 !important;
    }}

    [data-testid="stSidebar"] [data-testid="stFileUploaderFile"],
    [data-testid="stSidebar"] [data-testid="stFileUploaderFileName"] {{
        background: #263342 !important;
        color: #E7ECF2 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderFile"] * {{
        color: #E7ECF2 !important;
        fill: #E7ECF2 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stFileUploaderFile"] small {{
        color: #A9B6C4 !important;
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
    div[data-baseweb="popover"] li {{
        background: #263342 !important;
        color: #E7ECF2 !important;
    }}
    div[data-baseweb="popover"] li:hover {{
        background: #34465A !important;
    }}
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

    /* Tab bar: neutral by default, tint the active tab to match the rest
       of the palette instead of Streamlit's default red underline. */
    [data-testid="stTabs"] button[aria-selected="true"] {{
        color: {PRIMARY} !important;
        border-bottom-color: {PRIMARY} !important;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "ops_last" not in st.session_state:
    st.session_state["ops_last"] = None
if "ops_last_run_at" not in st.session_state:
    st.session_state["ops_last_run_at"] = None

# Context fields (populated from intake, not the model) vs. LLM-extracted
# fields with their own confidence score - mirrors Use Case 1's
# "Extracted Fields" / "System-Derived Metadata" split in the same spot.
_CONTEXT_METADATA_FIELDS = {"instruction_type", "email_date", "document_type", "source_document"}


def doc_status_chip(doc: OpsDocument) -> str:
    if doc.error:
        return '<span class="status-chip chip-error">ERROR</span>'
    if doc.doc_type_code == "UNRECOGNIZED":
        return '<span class="status-chip chip-warning">UNRECOGNIZED</span>'
    if doc.review_flags:
        return '<span class="status-chip chip-warning">NEEDS REVIEW</span>'
    return '<span class="status-chip chip-pass">OK</span>'


def _pack_label(pack: AuditPack) -> str:
    """Short table-cell label - same terse-word convention as Use Case 1's
    Status column (PASS/WARNING/FAIL), colour-mapped the same way via
    df.style.map rather than embedding HTML chips inside the dataframe."""
    if pack.is_complete:
        return "Complete"
    if not pack.items:
        return "No Pack Defined"
    return "Incomplete"


def _style_pack_label(val: str) -> str:
    color = {"Complete": PASS_C, "Incomplete": FAIL_C, "No Pack Defined": WARN_C}.get(val, MUTED)
    return f"color:{color}; font-weight:700;"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.image("assets/marvel_logo.png", width="stretch")
    st.markdown("---")
    st.markdown("### 📂 Intake")
    imap_ok = uc1_intake.imap_available()
    source_options = [
        "Upload files",
        "Use intake folder",
        "Email mailbox (IMAP)" + ("" if imap_ok else " (not configured)"),
    ]
    mode = st.radio("Source", source_options, label_visibility="collapsed")

    uploaded_files = None
    use_mailbox = False
    intake_dir = OPS_INTAKE_DIR
    if mode == "Upload files":
        uploaded_files = st.file_uploader(
            "Drop Withdrawal / Contribution PDFs and exported emails (.eml, .txt)",
            type=["pdf", "eml", "txt"], accept_multiple_files=True,
        )
        st.caption(
            "PDFs (instructions, trade orders, bank statements, ...), exported "
            "`.eml` emails (body + attachments both captured), or plain `.txt` "
            "notes — any mix, in one drop."
        )
    elif mode == "Use intake folder":
        intake_dir = Path(st.text_input("Intake folder path", value=str(OPS_INTAKE_DIR)))
        existing = (
            [p for p in intake_dir.iterdir() if p.suffix.lower() in (".pdf", ".eml", ".txt")]
            if intake_dir.exists() else []
        )
        st.caption(f"{len(existing)} document(s) found in folder.")
    else:
        use_mailbox = True
        if imap_ok:
            st.caption(f"Mailbox folder: `{settings.imap.folder}`  ·  Search: `{settings.imap.search_criteria}`")
        else:
            st.caption(
                "Set IMAP_HOST / IMAP_USERNAME / IMAP_PASSWORD as environment variables to enable "
                "this source (same mailbox settings as the UT app)."
            )

    st.markdown("---")
    run_clicked = st.button("▶  Process Batch", width="stretch")

    with st.expander("Document types in scope"):
        for d in load_document_types():
            st.markdown(f"**{d['code']}** — {d['name']}")

    with st.expander("Audit pack composition"):
        for tx_type, items in load_audit_pack_definitions().items():
            st.markdown(f"**{tx_type}**")
            for item in items:
                tag = "" if item.get("required", True) else " _(where applicable)_"
                st.markdown(f"- {item.get('note') or item['code']}{tag}")

    with st.expander("Salesperson roster (demo)"):
        st.caption(
            "No SharePoint/HR-directory integration yet - this is a synthetic "
            "roster standing in for one, only so the salesperson filter can be "
            "demonstrated. A document that states its own advisor/consultant "
            "always overrides this."
        )
        for person in load_salesperson_roster():
            st.markdown(f"**{person['name']}** — {', '.join(person.get('portfolios', []))}")

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="bifm-header">
        <div style="font-size:1.8rem;">🗂️</div>
        <div>
            <h1>BIFM Ops — Withdrawal &amp; Contribution Audit Automation</h1>
            <p>Intake · Classification &amp; Metadata Extraction · Correlation · Filing · Audit Packs · Search — POC</p>
        </div>
        <div class="bifm-badge">10 DOCUMENT TYPES · CENTRALIZED AUDIT REPOSITORY</div>
    </div>
    """,
    unsafe_allow_html=True,
)

process_tab, search_tab = st.tabs(["📥 Process a batch", "🔎 Search the repository"])

# --------------------------------------------------------------------------- #
# Process tab
# --------------------------------------------------------------------------- #
with process_tab:
    def _resolve_items():
        if mode == "Upload files":
            if not uploaded_files:
                return None, False
            ensure_output_dirs()
            for f in uploaded_files:
                (OPS_INTAKE_DIR / f.name).write_bytes(f.getbuffer())
            return OPS_INTAKE_DIR, False
        if mode == "Use intake folder":
            return intake_dir, False
        return None, True  # mailbox

    if run_clicked:
        from app.llm.router import check_connection, connection_error_message
        from ops.pipeline import run_ops_batch

        if not check_connection():
            st.error(connection_error_message())
            st.stop()

        run_intake_dir, run_use_mailbox = _resolve_items()
        if run_intake_dir is None and not run_use_mailbox:
            st.warning("Upload documents, point at a non-empty intake folder, or select the mailbox source first.")
            st.stop()

        progress_box = st.empty()
        log_box = st.empty()
        progress_bar = st.progress(0.0)

        # Thread-safe log queue: run_ops_batch's progress_cb is only ever
        # called from the thread that invoked it (analysis happens inside a
        # blocking ThreadPoolExecutor().map, not via callbacks from worker
        # threads) - but that call happens on a background thread here (so
        # the UI can keep redrawing the log live), so the same
        # queue-and-drain-on-the-main-thread pattern as Use Case 1 applies.
        import queue as _queue
        _log_q: _queue.Queue = _queue.Queue()

        def progress_cb(message: str) -> None:
            _log_q.put(message)

        def _drain_log(log_lines: list) -> None:
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

        log_lines: list[str] = []
        progress_box.info("Processing batch...")
        _batch_result: dict = {}

        def _run() -> None:
            documents, packs, report_path = run_ops_batch(
                intake_dir=run_intake_dir, use_mailbox=run_use_mailbox, progress_cb=progress_cb,
            )
            _batch_result["documents"] = documents
            _batch_result["packs"] = packs
            _batch_result["report_path"] = report_path

        worker_thread = threading.Thread(target=_run, daemon=True)
        worker_thread.start()
        total_docs = None
        done = 0
        while worker_thread.is_alive():
            before = len(log_lines)
            _drain_log(log_lines)
            for line in log_lines[before:]:
                m = re.search(r"Analysing (\d+) document", line)
                if m:
                    total_docs = int(m.group(1))
                if " -> " in line:  # one such line is emitted per analysed document
                    done += 1
            if total_docs:
                progress_bar.progress(min(done / total_docs, 0.99))
            time.sleep(0.3)
        worker_thread.join()
        _drain_log(log_lines)
        progress_bar.progress(1.0)

        documents = _batch_result.get("documents", [])
        packs = _batch_result.get("packs", [])
        report_path = _batch_result.get("report_path")

        progress_box.empty()
        progress_bar.empty()
        st.session_state["ops_last"] = {
            "documents": documents, "packs": packs,
            "report_path": str(report_path) if report_path else "",
        }
        st.session_state["ops_last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.success(f"Batch complete — {len(documents)} document(s) across {len(packs)} transaction(s).")
        st.rerun()

    last = st.session_state.get("ops_last")
    if not last:
        st.info("No batch has been run yet. Add documents in the sidebar and click **Process Batch** to begin.")
    else:
        all_documents: list[OpsDocument] = last["documents"]
        all_packs: list[AuditPack] = last["packs"]

        # Salesperson filter - scopes everything below it (metrics, the
        # transactions table, and Document Detail), the "filter ... based
        # on the salesperson's name" capability. Options come from THIS
        # batch's own transactions, not the full roster, so the dropdown
        # never offers a name that would filter down to nothing.
        batch_salespeople = sorted({p.transaction.salesperson for p in all_packs if p.transaction.salesperson})
        selected_salesperson = st.selectbox(
            "Filter by salesperson", [_ALL_SALESPEOPLE] + batch_salespeople,
        )
        if selected_salesperson == _ALL_SALESPEOPLE:
            packs = all_packs
            documents = all_documents
        else:
            packs = [p for p in all_packs if p.transaction.salesperson == selected_salesperson]
            scoped_keys = {p.transaction.transaction_key for p in packs}
            documents = [d for d in all_documents if d.transaction_key in scoped_keys]

        complete_n = sum(1 for p in packs if p.is_complete)
        incomplete_n = sum(1 for p in packs if not p.is_complete and p.items)
        review_n = sum(1 for d in documents if d.review_flags)

        cols = st.columns(5)
        metrics = [
            ("Documents processed", str(len(documents)), INK),
            ("Transactions",        str(len(packs)),      INK),
            ("Complete packs",      str(complete_n),      PASS_C),
            ("Incomplete packs",    str(incomplete_n),    FAIL_C),
            ("Needs review",        str(review_n),        WARN_C),
        ]
        for col, (label, value, color) in zip(cols, metrics):
            col.markdown(
                f'<div class="metric-card"><div class="label">{label}</div>'
                f'<div class="value" style="color:{color};">{value}</div></div>',
                unsafe_allow_html=True,
            )

        st.write("")

        type_counts: dict[str, int] = {}
        for p in packs:
            t = p.transaction.transaction_type or "Unknown"
            type_counts[t] = type_counts.get(t, 0) + 1
        if type_counts:
            st.markdown('<div class="section-title">Transaction Types</div>', unsafe_allow_html=True)
            breakdown_cols = st.columns(min(len(type_counts), 6))
            for col, (t, count) in zip(breakdown_cols, type_counts.items()):
                col.markdown(
                    f'<div class="metric-card"><div class="label">{t}</div>'
                    f'<div class="value" style="font-size:1.4rem;color:{INK};">{count}</div></div>',
                    unsafe_allow_html=True,
                )
            st.write("")

        left, right = st.columns([3, 2])

        with left:
            st.markdown('<div class="section-title">Transactions &amp; Audit Packs</div>', unsafe_allow_html=True)
            tx_rows = [{
                "Transaction": p.transaction.transaction_key,
                "Type": p.transaction.transaction_type,
                "Portfolio": p.transaction.portfolio_code,
                "Client": p.transaction.client_name,
                "Date": p.transaction.transaction_date,
                "Amount": p.transaction.transaction_amount,
                "Salesperson": p.transaction.salesperson or "—",
                "Docs": len(p.transaction.documents),
                "Pack": _pack_label(p),
            } for p in packs]
            if tx_rows:
                df = pd.DataFrame(tx_rows)
                st.dataframe(
                    df.style.map(_style_pack_label, subset=["Pack"]),
                    width="stretch", hide_index=True,
                )
            else:
                st.caption("No transactions in this batch.")

        with right:
            st.markdown('<div class="section-title">Download</div>', unsafe_allow_html=True)
            st.caption(f"Last run: {st.session_state.ops_last_run_at}")

            report_path = Path(last["report_path"]) if last["report_path"] else None
            if report_path and report_path.exists():
                st.download_button(
                    "⬇  Ops Report (Documents · Transactions · Audit Packs · Exceptions)",
                    data=report_path.read_bytes(), file_name=report_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch", key="download_ops_report", type="primary",
                )
                st.write("")

            if selected_salesperson != _ALL_SALESPEOPLE:
                from ops.audit_pack import zip_packs_for_salesperson
                zip_bytes = zip_packs_for_salesperson(all_packs, selected_salesperson)
                if zip_bytes:
                    st.download_button(
                        f"⬇  Audit Packs — {selected_salesperson} ({len(packs)} transaction(s))",
                        data=zip_bytes,
                        file_name=f"audit_packs_{selected_salesperson.replace(' ', '_')}.zip",
                        mime="application/zip", width="stretch", key="download_salesperson_zip",
                    )
                    st.write("")

            st.caption(
                "Every transaction's audit pack — the source documents plus a "
                "MANIFEST.txt stating exactly what's present and missing — is "
                "compiled automatically to `ops_output/audit_repository/<PortfolioCode>/...`"
            )
            filed_n = sum(1 for d in documents if d.filed_path)
            st.caption(f"📁 {filed_n} document(s) filed to `ops_output/filed/<Year>/<Month>/<Date>/<Transaction>/`")
            st.caption(f"🗄️ {len(packs)} audit pack(s) compiled to `ops_output/audit_repository/`")

        st.write("")
        st.markdown('<div class="section-title">Document Detail</div>', unsafe_allow_html=True)

        for doc in documents:
            chip = doc_status_chip(doc)
            with st.expander(
                f"{doc.source_file}  ·  {doc.doc_type_name}  ·  {doc.classification_confidence:.0f}% confidence"
            ):
                st.markdown(chip, unsafe_allow_html=True)
                sp = doc.meta("salesperson")
                sp_note = f" _({doc.salesperson_source})_" if sp and doc.salesperson_source else ""
                st.caption(
                    f"Transaction: `{doc.transaction_key or '—'}`  ·  Routed to: **{doc.assigned_team or '—'}**"
                    f"  ·  Salesperson: **{sp or '—'}**{sp_note}"
                )

                if doc.error:
                    st.error(doc.error)

                extracted_rows = [
                    {
                        "Field": fid.replace("_", " ").title(),
                        "Value": doc.meta(fid),
                        "Confidence": f"{doc.metadata_confidence.get(fid, 0):.0f}%" if fid in doc.metadata_confidence else "—",
                    }
                    for fid in METADATA_FIELDS
                    if fid not in _CONTEXT_METADATA_FIELDS and doc.meta(fid)
                ]
                context_rows = [
                    {"Field": fid.replace("_", " ").title(), "Value": doc.meta(fid)}
                    for fid in METADATA_FIELDS
                    if fid in _CONTEXT_METADATA_FIELDS and doc.meta(fid)
                ]

                if extracted_rows:
                    st.caption("**Extracted Metadata**")
                    st.dataframe(pd.DataFrame(extracted_rows), width="stretch", hide_index=True)
                if context_rows:
                    st.caption("**Intake Context**")
                    st.dataframe(pd.DataFrame(context_rows), width="stretch", hide_index=True)

                if doc.review_flags:
                    st.caption("**Needs Review**")
                    for flag in doc.review_flags:
                        st.warning(flag, icon="⚠️")
                elif not doc.error:
                    st.success("No review flags raised.", icon="✅")

                if doc.filed_path:
                    st.caption(f"Filed: `{doc.filed_path}`")

# --------------------------------------------------------------------------- #
# Search tab
# --------------------------------------------------------------------------- #
with search_tab:
    from ops.repository import documents_for_transaction, list_salespeople, list_transactions, search_documents

    st.markdown('<div class="section-title">Search the Metadata Repository</div>', unsafe_allow_html=True)
    st.caption(
        "Search by any extracted metadata — Portfolio Code / Name, Client Name, "
        "Transaction Type / Date / Amount, Trade ID, Security Name / Code — across every run."
    )
    col_q, col_f = st.columns([3, 1])
    query = col_q.text_input("Search", placeholder="e.g. BPMMF01, Theo Mabe, TRD-2026-001, 25000")
    field = col_f.selectbox("Field", ["(any field)"] + METADATA_FIELDS)

    if query:
        rows = search_documents(query, field=None if field == "(any field)" else field)
        st.caption(f"{len(rows)} document(s) matched.")
        if rows:
            df = pd.DataFrame(rows)[
                ["source_file", "doc_type_name", "transaction_key", "portfolio_code",
                 "client_name", "transaction_type", "transaction_date", "transaction_amount",
                 "trade_id", "filed_path"]
            ]
            st.dataframe(df, width="stretch", hide_index=True)

    st.write("")
    st.markdown('<div class="section-title">All Transactions</div>', unsafe_allow_html=True)
    known_salespeople = list_salespeople()
    salesperson_filter = st.selectbox(
        "Filter by salesperson", [_ALL_SALESPEOPLE] + known_salespeople, key="search_salesperson_filter",
    )
    tx = list_transactions(
        salesperson=None if salesperson_filter == _ALL_SALESPEOPLE else salesperson_filter,
    )
    if tx:
        st.dataframe(pd.DataFrame(tx), width="stretch", hide_index=True)
        picked = st.selectbox("Show documents for transaction", [""] + [t["transaction_key"] for t in tx])
        if picked:
            docs = documents_for_transaction(picked)
            st.dataframe(pd.DataFrame(docs)[
                ["source_file", "doc_type_name", "portfolio_code", "transaction_amount", "filed_path"]
            ], width="stretch", hide_index=True)
    elif salesperson_filter != _ALL_SALESPEOPLE:
        st.caption(f"No transactions found for {salesperson_filter}.")
    else:
        st.caption("Repository is empty — process a batch first.")
