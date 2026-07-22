"""
Config loaders + output paths for Use Case 2 (BIFM Ops).

Everything configurable lives in ops/config/*.json - document types and
audit-pack composition, workflow routing, correlation identifiers, folder
naming conventions, the client-provided Portfolio Name-to-Code mapping,
and the (demo-only, see salesperson_roster.json) salesperson roster - so
a behaviour change is a JSON edit, not a code change (same
configuration-driven principle as Use Case 1's config/ directory).

Output tree (separate from UC1's output/ so the two apps never collide):
    ops_output/
      intake/            drop folder for local intake
      filed/             Year/Month/Date/Transaction structure (per BIFM rules)
      audit_repository/  centralized audit packs by Portfolio Code
      ops_metadata.db    the SQLite metadata repository
      reports/           Excel dashboard per run
"""
from __future__ import annotations

import csv
import json
import os
from functools import lru_cache
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_BASE_DIR = Path(__file__).resolve().parent.parent

OPS_OUTPUT_DIR = Path(os.environ.get("OPS_OUTPUT_DIR", _BASE_DIR / "ops_output"))
OPS_INTAKE_DIR = OPS_OUTPUT_DIR / "intake"
OPS_FILED_DIR = OPS_OUTPUT_DIR / "filed"
OPS_AUDIT_DIR = OPS_OUTPUT_DIR / "audit_repository"
OPS_REPORTS_DIR = OPS_OUTPUT_DIR / "reports"
OPS_DB_PATH = OPS_OUTPUT_DIR / "ops_metadata.db"

OPS_REPORT_NAME = "BIFM_Ops_Audit_Automation_Report.xlsx"


def ensure_output_dirs() -> None:
    for p in (OPS_INTAKE_DIR, OPS_FILED_DIR, OPS_AUDIT_DIR, OPS_REPORTS_DIR):
        p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=None)
def _load_json(filename: str) -> dict:
    with open(_CONFIG_DIR / filename, encoding="utf-8") as fh:
        return json.load(fh)


def load_document_types() -> list[dict]:
    return _load_json("ops_document_types.json")["document_types"]


def load_audit_pack_definitions() -> dict[str, list[dict]]:
    return _load_json("ops_document_types.json")["audit_packs"]


def load_workflow() -> dict:
    return _load_json("ops_workflow.json")


def load_portfolio_mapping() -> list[dict]:
    """The client-provided Portfolio Name-to-Code mapping. Defaults to
    ops/config/portfolio_mapping.json; point OPS_PORTFOLIO_MAPPING at a
    client-supplied CSV (name,code[,aliases - ';'-separated]) to swap in
    the real mapping without touching the repo."""
    override = os.environ.get("OPS_PORTFOLIO_MAPPING", "")
    if override and override.lower().endswith(".csv") and Path(override).exists():
        rows: list[dict] = []
        with open(override, encoding="utf-8-sig", newline="") as fh:
            for record in csv.DictReader(fh):
                name = (record.get("name") or "").strip()
                code = (record.get("code") or "").strip()
                if not name or not code:
                    continue
                aliases = [a.strip() for a in (record.get("aliases") or "").split(";") if a.strip()]
                rows.append({"name": name, "code": code, "aliases": aliases})
        if rows:
            return rows
    return _load_json("portfolio_mapping.json")["portfolios"]


def load_salesperson_roster() -> list[dict]:
    """DEMO roster only - see ops/config/salesperson_roster.json's own
    comment. Defaults to that file; point OPS_SALESPERSON_ROSTER at a
    client-supplied CSV (name,portfolios - ';'-separated portfolio codes)
    to swap in a real SharePoint/HR-directory feed without touching the
    repo, the same override pattern as load_portfolio_mapping()."""
    override = os.environ.get("OPS_SALESPERSON_ROSTER", "")
    if override and override.lower().endswith(".csv") and Path(override).exists():
        rows: list[dict] = []
        with open(override, encoding="utf-8-sig", newline="") as fh:
            for record in csv.DictReader(fh):
                name = (record.get("name") or "").strip()
                if not name:
                    continue
                portfolios = [p.strip() for p in (record.get("portfolios") or "").split(";") if p.strip()]
                rows.append({"name": name, "portfolios": portfolios})
        if rows:
            return rows
    return _load_json("salesperson_roster.json")["salespeople"]


def document_type_lookup() -> dict[str, str]:
    """code -> display name."""
    return {d["code"]: d["name"] for d in load_document_types()}


def clear_config_cache() -> None:
    _load_json.cache_clear()
