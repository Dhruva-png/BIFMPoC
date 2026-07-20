"""
Portfolio Name -> Portfolio Code resolution for Use Case 2.

Straight from the proposal: "Utilize the client-provided Portfolio
Name-to-Portfolio Code mapping to identify and correlate documents where
standardized portfolio codes are not available in incoming instructions."

Clients write portfolio names loosely in emails ("the Pula money market
fund", "GSGF") rather than the standardized code, so resolution is layered:
exact code, exact name, alias, substring containment, then a close fuzzy
match - and, like Use Case 1's field-repair layer, it only ever accepts an
UNAMBIGUOUS match. A name that could be two portfolios is left unresolved
and flagged for human review rather than guessed.
"""
from __future__ import annotations

import difflib
import re

from ops.ops_config import load_portfolio_mapping


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def resolve_portfolio(name_or_code: str | None) -> tuple[str, str]:
    """
    Returns (portfolio_code, canonical_portfolio_name), or ("", "") when
    the input is blank or can't be resolved unambiguously.
    """
    if not name_or_code or not str(name_or_code).strip():
        return "", ""
    text = _norm(name_or_code)
    portfolios = load_portfolio_mapping()

    # 1. Exact code (the standardized identifier, when the client did use it).
    for p in portfolios:
        if text == _norm(p["code"]):
            return p["code"], p["name"]

    # 2. Exact canonical name or alias.
    for p in portfolios:
        if text == _norm(p["name"]) or any(text == _norm(a) for a in p.get("aliases", [])):
            return p["code"], p["name"]

    # 3. Unambiguous containment either way ("the Pula money market fund
    #    please" contains the alias "Pula Money Market").
    contained = []
    for p in portfolios:
        candidates = [p["name"]] + list(p.get("aliases", []))
        if any(_norm(c) in text or text in _norm(c) for c in candidates):
            contained.append(p)
    if len(contained) == 1:
        return contained[0]["code"], contained[0]["name"]
    if len(contained) > 1:
        return "", ""  # ambiguous - a human decides, we don't guess

    # 4. Close fuzzy match against names + aliases, single candidate only.
    label_to_portfolio: dict[str, dict] = {}
    for p in portfolios:
        for label in [p["name"]] + list(p.get("aliases", [])):
            label_to_portfolio[_norm(label)] = p
    close = difflib.get_close_matches(text, list(label_to_portfolio), n=2, cutoff=0.75)
    matched = {id(label_to_portfolio[c]): label_to_portfolio[c] for c in close}
    if len(matched) == 1:
        p = next(iter(matched.values()))
        return p["code"], p["name"]

    return "", ""
