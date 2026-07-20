"""
The centralized metadata repository for Use Case 2.

"Intelligently extract, validate, and maintain the metadata below to
enable classification, correlation, and retrieval; the metadata
repository serves as the basis for document search and audit compilation"
and "Provide intelligent search across the configured repositories using
any of the extracted metadata (Portfolio Code / Name, Client Name,
Transaction Type / Date / Amount, Trade ID, Security Name / Code),
retrieving all documents associated with a transaction or portfolio."

SQLite (stdlib, zero new dependencies) with one row per document carrying
every metadata field as its own column, so search works field-scoped
("portfolio_code = BPMMF01") or across everything at once (free text).
The repository persists across runs - each batch upserts by filed path -
so retrieval spans the whole history, not just the last batch.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.utils.logger import get_logger
from ops.models import METADATA_FIELDS, AuditPack, OpsDocument
from ops.ops_config import OPS_DB_PATH, ensure_output_dirs

logger = get_logger(__name__)

_DOC_COLUMNS = [
    "source_file", "source_path", "filed_path", "doc_type_code", "doc_type_name",
    "classification_confidence", "transaction_key", "assigned_team", "review_flags",
] + METADATA_FIELDS

_SEARCHABLE_FIELDS = [
    "source_file", "doc_type_code", "doc_type_name", "transaction_key", "assigned_team",
] + METADATA_FIELDS


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_output_dirs()
    conn = sqlite3.connect(db_path or OPS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn: sqlite3.Connection) -> None:
    doc_cols = ", ".join(f"{c} TEXT" for c in _DOC_COLUMNS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {doc_cols},
            UNIQUE(filed_path)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_key TEXT PRIMARY KEY,
            transaction_type TEXT, portfolio_code TEXT, portfolio_name TEXT,
            client_name TEXT, transaction_date TEXT, transaction_amount TEXT,
            trade_id TEXT, pack_status TEXT, audit_folder TEXT, document_count TEXT
        )""")
    conn.commit()


def save_batch(documents: list[OpsDocument], packs: list[AuditPack], db_path: Path | None = None) -> None:
    """Upserts this run's documents and transactions into the repository."""
    conn = _connect(db_path)
    try:
        _init(conn)
        for doc in documents:
            values = {
                "source_file": doc.source_file,
                "source_path": doc.source_path,
                "filed_path": doc.filed_path or doc.source_path,
                "doc_type_code": doc.doc_type_code,
                "doc_type_name": doc.doc_type_name,
                "classification_confidence": f"{doc.classification_confidence:.0f}",
                "transaction_key": doc.transaction_key,
                "assigned_team": doc.assigned_team,
                "review_flags": json.dumps(doc.review_flags),
            }
            for field_id in METADATA_FIELDS:
                values[field_id] = doc.meta(field_id)
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != "filed_path")
            conn.execute(
                f"INSERT INTO documents ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(filed_path) DO UPDATE SET {updates}",
                list(values.values()),
            )
        for pack in packs:
            tx = pack.transaction
            conn.execute(
                "INSERT INTO transactions (transaction_key, transaction_type, portfolio_code, "
                "portfolio_name, client_name, transaction_date, transaction_amount, trade_id, "
                "pack_status, audit_folder, document_count) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(transaction_key) DO UPDATE SET transaction_type=excluded.transaction_type, "
                "portfolio_code=excluded.portfolio_code, portfolio_name=excluded.portfolio_name, "
                "client_name=excluded.client_name, transaction_date=excluded.transaction_date, "
                "transaction_amount=excluded.transaction_amount, trade_id=excluded.trade_id, "
                "pack_status=excluded.pack_status, audit_folder=excluded.audit_folder, "
                "document_count=excluded.document_count",
                (tx.transaction_key, tx.transaction_type, tx.portfolio_code, tx.portfolio_name,
                 tx.client_name, tx.transaction_date, tx.transaction_amount, tx.trade_id,
                 pack.status, pack.audit_folder, str(len(tx.documents))),
            )
        conn.commit()
        logger.info("Repository: saved %d document(s), %d transaction(s)", len(documents), len(packs))
    finally:
        conn.close()


def search_documents(query: str, field: str | None = None, db_path: Path | None = None) -> list[dict]:
    """Search by any extracted metadata. field=None searches every
    searchable column at once (free text); a named field scopes the search
    ("portfolio_code", "client_name", "trade_id", ...)."""
    conn = _connect(db_path)
    try:
        _init(conn)
        like = f"%{query}%"
        if field:
            if field not in _SEARCHABLE_FIELDS:
                raise ValueError(f"Unknown search field '{field}'. Searchable: {', '.join(_SEARCHABLE_FIELDS)}")
            where = f"{field} LIKE ?"
            params: list = [like]
        else:
            where = " OR ".join(f"{c} LIKE ?" for c in _SEARCHABLE_FIELDS)
            params = [like] * len(_SEARCHABLE_FIELDS)
        rows = conn.execute(
            f"SELECT * FROM documents WHERE {where} ORDER BY transaction_key, doc_type_code",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def documents_for_transaction(transaction_key: str, db_path: Path | None = None) -> list[dict]:
    """All documents associated with one transaction - 'retrieving all
    documents associated with a transaction or portfolio'."""
    conn = _connect(db_path)
    try:
        _init(conn)
        rows = conn.execute(
            "SELECT * FROM documents WHERE transaction_key = ? ORDER BY doc_type_code",
            (transaction_key,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_transactions(db_path: Path | None = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        _init(conn)
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY transaction_date DESC, transaction_key"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
