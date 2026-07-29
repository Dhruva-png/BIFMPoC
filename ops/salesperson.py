"""
Salesperson resolution for Use Case 2 - DEMO CAPABILITY.

There is no SharePoint/HR-directory integration for salesperson assignment
in this PoC, and no real BIFM sales-consultant roster to build one
against. This module exists so the "filter and compile audit packs by
salesperson" capability can be shown end-to-end anyway, using a synthetic
roster (ops/config/salesperson_roster.json) as a stand-in for that future
integration - see load_salesperson_roster()'s docstring for how to point
this at a real feed later (OPS_SALESPERSON_ROSTER).

Three-tier resolution, in priority order:
  1. EXTRACTED - the document itself states a sales consultant / advisor /
     broker name (ops/analyzer.py asks for this the same way it asks for
     every other metadata field). Real data always wins.
  2. ASSIGNED (demo roster) - when nothing is stated, fall back to the
     roster's portfolio -> salesperson mapping, once the portfolio itself
     has resolved (ops.portfolio.resolve_portfolio).
  3. ASSIGNED (demo roster), at random - when neither of the above yields
     anyone (no stated name, and either no portfolio or a portfolio the
     roster doesn't cover), pick uniformly from the roster anyway, so
     every pack built in the demo has a named salesperson to show rather
     than a blank field. This is explicitly a demo-only relaxation of the
     "never assign without a real peg" rule ops.portfolio still follows -
     there being no visible salesperson isn't useful for demonstrating the
     "filter and compile audit packs by salesperson" capability end-to-end.

The (name, source) return lets callers show a document's salesperson
alongside whether it was actually stated or just demo-assigned - the
distinction matters for anyone deciding how much to trust the field.
"""
from __future__ import annotations

import random

from ops.ops_config import load_salesperson_roster

SOURCE_EXTRACTED = "Extracted"
SOURCE_ASSIGNED = "Assigned (demo roster)"
SOURCE_NONE = ""


def resolve_salesperson(stated_name: str | None, portfolio_code: str | None) -> tuple[str, str]:
    """
    Returns (salesperson_name, source). source is SOURCE_EXTRACTED when
    stated_name was used as-is, SOURCE_ASSIGNED when it came from the demo
    roster (via portfolio_code if possible, else at random), or
    SOURCE_NONE ("", "") only when the roster itself is empty.
    """
    if stated_name and str(stated_name).strip():
        return str(stated_name).strip(), SOURCE_EXTRACTED

    roster = load_salesperson_roster()
    if not roster:
        return "", SOURCE_NONE

    if portfolio_code:
        for person in roster:
            if portfolio_code in person.get("portfolios", []):
                return person["name"], SOURCE_ASSIGNED

    return random.choice(roster)["name"], SOURCE_ASSIGNED
