"""
CLI entry point for Use Case 2 - BIFM Ops: Withdrawal & Contribution
Audit Document Automation.

Usage:
    python ops_main.py run --intake ./ops_intake          # process a folder
    python ops_main.py run --mailbox                      # pull the IMAP mailbox
    python ops_main.py search "BPMMF01"                   # free-text metadata search
    python ops_main.py search "Theo" --field client_name  # field-scoped search
    python ops_main.py transactions                       # list known transactions

The Streamlit UI equivalent is: streamlit run ops_app.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger("ops_main")


def _cmd_run(args: argparse.Namespace) -> int:
    from app.llm.router import check_connection
    from ops.pipeline import run_ops_batch

    if not check_connection():
        print("ERROR: Could not reach Groq (missing or invalid GROQ_API_KEY). "
              "Get a free key (no card) at https://console.groq.com/keys and set it in "
              "your .env file (see .env.example) or as an environment variable.")
        return 1

    intake = Path(args.intake) if args.intake else None
    if intake is None and not args.mailbox:
        print("Nothing to do: pass --intake <folder> and/or --mailbox")
        return 1

    documents, packs, report_path = run_ops_batch(
        intake_dir=intake, use_mailbox=args.mailbox, progress_cb=print,
    )

    print("\n--- Ops Batch Summary ---")
    for pack in packs:
        tx = pack.transaction
        print(f"{tx.transaction_key:30s} {tx.transaction_type:12s} "
              f"{tx.portfolio_code:10s} docs={len(tx.documents)}  {pack.status}")
    flagged = [d for d in documents if d.review_flags]
    if flagged:
        print(f"\n{len(flagged)} document(s) need review - see the Exceptions sheet.")
    print(f"\nReport: {report_path}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from ops.repository import search_documents

    rows = search_documents(args.query, field=args.field)
    if not rows:
        print("No documents matched.")
        return 0
    for row in rows:
        print(f"{row['transaction_key']:30s} {row['doc_type_name']:38s} "
              f"{row['portfolio_code']:10s} {row['client_name'] or '':20s} {row['filed_path']}")
    print(f"\n{len(rows)} document(s).")
    return 0


def _cmd_transactions(_args: argparse.Namespace) -> int:
    from ops.repository import list_transactions

    rows = list_transactions()
    if not rows:
        print("No transactions in the repository yet.")
        return 0
    for row in rows:
        print(f"{row['transaction_key']:30s} {row['transaction_type'] or '':12s} "
              f"{row['portfolio_code'] or '':10s} {row['transaction_date'] or '':12s} "
              f"{row['transaction_amount'] or '':>12s}  {row['pack_status']}")
    print(f"\n{len(rows)} transaction(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BIFM Ops - Withdrawal & Contribution Audit Document Automation (Use Case 2)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Process an intake batch")
    run_p.add_argument("--intake", type=str, default=None, help="Folder of PDFs / EMLs / TXTs")
    run_p.add_argument("--mailbox", action="store_true", help="Also pull the configured IMAP mailbox")
    run_p.set_defaults(func=_cmd_run)

    search_p = sub.add_parser("search", help="Search the metadata repository")
    search_p.add_argument("query", type=str)
    search_p.add_argument("--field", type=str, default=None,
                          help="Scope to one metadata field (e.g. portfolio_code, client_name, trade_id)")
    search_p.set_defaults(func=_cmd_search)

    tx_p = sub.add_parser("transactions", help="List known transactions and pack status")
    tx_p.set_defaults(func=_cmd_transactions)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
