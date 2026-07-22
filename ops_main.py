"""
CLI entry point for Use Case 2 - BIFM Ops: Withdrawal & Contribution
Audit Document Automation.

Usage:
    python ops_main.py run --intake ./ops_intake          # process a folder
    python ops_main.py run --mailbox                      # pull the IMAP mailbox
    python ops_main.py search "BPMMF01"                   # free-text metadata search
    python ops_main.py search "Theo" --field client_name  # field-scoped search
    python ops_main.py transactions                       # list known transactions
    python ops_main.py transactions --salesperson "Thabo Kgosi"   # filter by salesperson (demo capability - see ops/salesperson.py)
    python ops_main.py export-packs "Thabo Kgosi" -o packs.zip    # zip that salesperson's compiled audit packs

The Streamlit UI equivalent is: streamlit run ops_app.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger("ops_main")


def _cmd_run(args: argparse.Namespace) -> int:
    from app.llm.router import check_connection, connection_error_message
    from ops.pipeline import run_ops_batch

    if not check_connection():
        print(f"ERROR: {connection_error_message()}")
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


def _cmd_transactions(args: argparse.Namespace) -> int:
    from ops.repository import list_transactions

    rows = list_transactions(salesperson=args.salesperson or None)
    if not rows:
        if args.salesperson:
            print(f"No transactions found for {args.salesperson}.")
        else:
            print("No transactions in the repository yet.")
        return 0
    for row in rows:
        print(f"{row['transaction_key']:30s} {row['transaction_type'] or '':12s} "
              f"{row['portfolio_code'] or '':10s} {row['transaction_date'] or '':12s} "
              f"{row['transaction_amount'] or '':>12s}  {row['salesperson'] or '':16s} {row['pack_status']}")
    print(f"\n{len(rows)} transaction(s).")
    return 0


def _cmd_export_packs(args: argparse.Namespace) -> int:
    """Zips every already-compiled audit pack for one salesperson - the
    CLI form of the UI's scoped download button. Reads folder paths back
    from the repository (not live AuditPack objects, which don't outlive
    the run that created them), so this works against ANY past run, not
    just the one you just processed."""
    from ops.audit_pack import zip_audit_folders
    from ops.repository import list_transactions

    rows = list_transactions(salesperson=args.salesperson)
    if not rows:
        print(f"No transactions found for {args.salesperson}.")
        return 1

    # A valid empty zip is still non-empty bytes (the End-Of-Central-
    # Directory record), so check folder existence up front rather than
    # trusting zip_bytes' truthiness to tell us whether anything was
    # actually included.
    existing_folders = [r["audit_folder"] for r in rows if r["audit_folder"] and Path(r["audit_folder"]).exists()]
    if not existing_folders:
        print(f"No compiled audit packs found on disk for {args.salesperson}.")
        return 1

    zip_bytes = zip_audit_folders(existing_folders)
    out_path = Path(args.output)
    out_path.write_bytes(zip_bytes)
    print(f"Wrote {len(rows)} transaction(s)' audit packs -> {out_path}")
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
    tx_p.add_argument("--salesperson", type=str, default=None,
                       help="Filter to one salesperson (demo capability - see ops/salesperson.py)")
    tx_p.set_defaults(func=_cmd_transactions)

    export_p = sub.add_parser(
        "export-packs", help="Zip every compiled audit pack for one salesperson (demo capability)")
    export_p.add_argument("salesperson", type=str)
    export_p.add_argument("-o", "--output", type=str, default="audit_packs.zip")
    export_p.set_defaults(func=_cmd_export_packs)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
