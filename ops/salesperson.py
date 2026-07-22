"""
Salesperson resolution for Use Case 2 - DEMO CAPABILITY.

There is no SharePoint/HR-directory integration for salesperson assignment
in this PoC, and no real BIFM sales-consultant roster to build one
against. This module exists so the "filter and compile audit packs by
salesperson" capability can be shown end-to-end anyway, using a synthetic
roster (ops/config/salesperson_roster.json) as a stand-in for that future
integration - see load_salesperson_roster()'s docstring for how to point
this at a real feed later (OPS_SALESPERSON_ROSTER).

Two-tier resolution, in priority order:
  1. EXTRACTED - the document itself states a sales consultant / advisor /
     broker name (ops/analyzer.py asks for this the same way it asks for
     every other metadata field). Real data always wins.
  2. ASSIGNED (demo roster) - when nothing is stated, fall back to the
     roster's portfolio -> salesperson mapping. Only possible once the
     portfolio itself has resolved (ops.portfolio.resolve_portfolio) -
     a portfolio-less document gets no salesperson rather than a guess,
     the same "never assign without a real peg" rule ops.portfolio uses.

The (name, source) return lets callers show a document's salesperson
alongside whether it was actually stated or just demo-assigned - the
distinction matters for anyone deciding how much to trust the field.
"""
from __future__ import annotations

from ops.ops_config import load_salesperson_roster

SOURCE_EXTRACTED = "Extracted"
SOURCE_ASSIGNED = "Assigned (demo roster)"
SOURCE_NONE = ""


def resolve_salesperson(stated_name: str | None, portfolio_code: str | None) -> tuple[str, str]:
    """
    Returns (salesperson_name, source). source is SOURCE_EXTRACTED when
    stated_name was used as-is, SOURCE_ASSIGNED when it came from the demo
    roster via portfolio_code, or SOURCE_NONE ("", "") when neither yields
    a name.
    """
    if stated_name and str(stated_name).strip():
        return str(stated_name).strip(), SOURCE_EXTRACTED

    if not portfolio_code:
        return "", SOURCE_NONE

    for person in load_salesperson_roster():
        if portfolio_code in person.get("portfolios", []):
            return person["name"], SOURCE_ASSIGNED

    return "", SOURCE_NONE
