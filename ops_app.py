"""
Streamlit UI for Use Case 2 - BIFM Ops: Withdrawal & Contribution Audit
Document Automation.

    streamlit run ops_app.py

Two tabs:
  Process - drop PDFs / exported .eml emails (or point at a folder /
            the configured IMAP mailbox), run the batch, review
            transactions, pack completeness, and exceptions.
  Search  - the metadata repository: free-text or field-scoped search
            across every extracted metadata field, spanning all runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings  # noqa: E402
from ops.models import METADATA_FIELDS  # noqa: E402
from ops.ops_config import OPS_INTAKE_DIR, ensure_output_dirs  # noqa: E402

st.set_page_config(page_title="BIFM Ops - Audit Document Automation", page_icon="🗂️", layout="wide")

st.title("🗂️ BIFM Ops — Withdrawal & Contribution Audit Document Automation")
st.caption(
    "Use Case 2: email/document intake → classification & metadata extraction "
    "(Portfolio Name→Code mapping) → team routing → transaction correlation → "
    "Year/Month/Date/Transaction filing → audit-pack compilation → searchable metadata repository."
)

process_tab, search_tab = st.tabs(["📥 Process a batch", "🔎 Search the repository"])

with process_tab:
    left, right = st.columns([1, 2])

    with left:
        st.subheader("Intake")
        uploaded = st.file_uploader(
            "Drop Withdrawal / Contribution documents (PDF) and exported emails (.eml, .txt)",
            type=["pdf", "eml", "txt"], accept_multiple_files=True,
        )
        use_mailbox = st.checkbox(
            "Also pull the configured email mailbox (IMAP)",
            help="Uses the same IMAP_* settings as the UT app. Disabled if not configured.",
        )
        run_clicked = st.button("▶  Process batch", type="primary", width="stretch")

    with right:
        st.subheader("Progress")
        log_area = st.empty()

    if run_clicked:
        from app.llm.router import check_connection
        from ops.pipeline import run_ops_batch

        if not check_connection():
            st.error("Could not reach Groq — set GROQ_API_KEY in your .env (see .env.example).")
            st.stop()

        ensure_output_dirs()
        batch_dir = OPS_INTAKE_DIR
        for f in uploaded or []:
            (batch_dir / f.name).write_bytes(f.getbuffer())

        log_lines: list[str] = []

        def progress(message: str) -> None:
            log_lines.append(message)
            log_area.code("\n".join(log_lines[-25:]))

        if not (uploaded or use_mailbox):
            st.warning("Upload documents or tick the mailbox option first.")
            st.stop()

        with st.spinner("Processing..."):
            documents, packs, report_path = run_ops_batch(
                intake_dir=batch_dir if uploaded else None,
                use_mailbox=use_mailbox,
                progress_cb=progress,
            )

        st.session_state["ops_last"] = {
            "documents": documents, "packs": packs, "report_path": str(report_path),
        }

    last = st.session_state.get("ops_last")
    if last:
        documents, packs = last["documents"], last["packs"]
        st.divider()
        st.subheader("Transactions & audit packs")
        tx_rows = [{
            "Transaction": p.transaction.transaction_key,
            "Type": p.transaction.transaction_type,
            "Portfolio": p.transaction.portfolio_code,
            "Client": p.transaction.client_name,
            "Date": p.transaction.transaction_date,
            "Amount": p.transaction.transaction_amount,
            "Docs": len(p.transaction.documents),
            "Pack": p.status,
        } for p in packs]
        if tx_rows:
            st.dataframe(pd.DataFrame(tx_rows), width="stretch", hide_index=True)

        flagged = [d for d in documents if d.review_flags]
        if flagged:
            st.subheader(f"⚠️ Needs review ({len(flagged)})")
            st.dataframe(pd.DataFrame([{
                "File": d.source_file, "Type": d.doc_type_name,
                "Transaction": d.transaction_key, "Flags": "; ".join(d.review_flags),
            } for d in flagged]), width="stretch", hide_index=True)

        report_path = Path(last["report_path"])
        if report_path.exists():
            st.download_button(
                "⬇  Download Ops report (Documents · Transactions · Audit Packs · Exceptions)",
                data=report_path.read_bytes(), file_name=report_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

with search_tab:
    from ops.repository import documents_for_transaction, list_transactions, search_documents

    st.subheader("Search the metadata repository")
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

    st.divider()
    st.subheader("All transactions")
    tx = list_transactions()
    if tx:
        st.dataframe(pd.DataFrame(tx), width="stretch", hide_index=True)
        picked = st.selectbox("Show documents for transaction", [""] + [t["transaction_key"] for t in tx])
        if picked:
            docs = documents_for_transaction(picked)
            st.dataframe(pd.DataFrame(docs)[
                ["source_file", "doc_type_name", "portfolio_code", "transaction_amount", "filed_path"]
            ], width="stretch", hide_index=True)
    else:
        st.caption("Repository is empty — process a batch first.")
