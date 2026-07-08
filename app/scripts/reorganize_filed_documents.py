"""
One-off migration script: reorganizes an EXISTING, already-messy
filed_documents tree into the canonical structure (matches the reference
diagram provided for Disinvestments — the same shape applies to every
instruction type):

    <FormCategory>/<Year>/<Month-Name>/<DD-Mon-YYYY>/
        Money Market (cut-off 12:00 PM)/        <- MM/NMM types only
            <raw captured pdf + _Extracted.xlsx pairs — Marvel.ai output>
        Non-Money Market (cut-off 3:00 PM)/      <- MM/NMM types only
            <raw captured pdf + _Extracted.xlsx pairs — Marvel.ai output>
        Captured/
        Approved/
        Rejected/
            <renamed pdf, rejection reason appended to filename>

This is a ONE-TIME cleanup of files that already exist on disk from earlier,
inconsistent runs. It does not change app code — see app/services/filer.py
for the fix that stops NEW files from landing in the wrong place going
forward. Run this once against old output, then rely on filer.py for
everything from here on.

WHAT IT FIXES
--------------
1. Fund-bucket folder naming collapsed onto one canonical name per bucket
   ("Money Market (cut-off 12:00 PM)" / "Non-Money Market (cut-off 3:00
   PM)"), merging every variant seen in the wild: "MoneyMarket",
   "Money Market", "NonMoneyMarket", "Non-Money Market", etc.
2. Captured / Approved / Rejected folders — wherever they were nested
   (inside a fund bucket, inside a status-within-status path, etc.) — are
   hoisted to be flat siblings directly under the date folder, never
   inside a fund-bucket folder, matching the reference diagram.
3. Date folders normalized to "DD-Mon-YYYY" (e.g. "09-Jul-2026").
4. Every "Missing" marker file, wherever it was nested, is merged into ONE
   top-level "Missing" folder per form category (not per fund-bucket/date).
5. Any stray top-level date-only folder (a bare "2026" sibling of
   "Disinvestments" etc., with no category prefix) is treated as orphaned
   Disinvestments data (it matches the DIS dated-folder shape) and merged
   in under Disinvestments. This is flagged loudly in the log either way
   so a human can double-check the call.

SAFE BY DEFAULT: this script defaults to --dry-run, which only prints what
it *would* do. Nothing is moved until you pass --apply. See "HOW TO TEST"
at the bottom of this file / the accompanying chat message.
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("reorganize_filed_documents")

# ---------------------------------------------------------------------------
# Canonical vocabulary
# ---------------------------------------------------------------------------

MONEY_MARKET_CANONICAL = "Money Market  (cut-off 12:00 PM)"
NON_MONEY_MARKET_CANONICAL = "Non-Money Market  (cut-off 3:00 PM)"

STATUS_DIRS = {"Captured", "Approved", "Rejected"}
MISSING_DIR = "Missing"

# Every top-level instruction-type folder this app produces. Any of these
# get the full date/fund-bucket/status treatment.
FORM_CATEGORIES = {
    "New Business",
    "Additional Investments",
    "Disinvestments",
    "Debit Orders",
    "Static",
    "KYC",
}

# Categories that split by Money Market / Non-Money Market cut-off.
FUND_BUCKET_CATEGORIES = {"Additional Investments", "Disinvestments"}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_LOOKUP = {name.lower(): i + 1 for i, name in enumerate(MONTH_NAMES)}
MONTH_ABBR_LOOKUP = {name[:3].lower(): i + 1 for i, name in enumerate(MONTH_NAMES)}

_YEAR_RE = re.compile(r"^(20\d{2})$")
_MM_DASH_MONTH_RE = re.compile(r"^(\d{2})-([A-Za-z]+)$")           # "07-July"
_DAY_ONLY_RE = re.compile(r"^(\d{1,2})$")                           # "07"
_DAY_MON_YEAR_RE = re.compile(r"^(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](20\d{2})$")  # "09-Jul-2026"
_MM_DD_YYYY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(20\d{2})$")     # "07-08-2026" (ambiguous!)
_QUARTER_END_RE = re.compile(r"^\d{1,2}-[A-Za-z]{3}-20\d{2}$")


def _is_money_market_dirname(name: str) -> str | None:
    """Returns 'MM', 'NMM', or None (not a fund-bucket folder at all)."""
    n = name.lower().replace("-", " ").replace("_", " ")
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\(.*?\)", "", n).strip()  # drop "(cut-off ...)" annotations
    if "non money market" in n:
        return "NMM"
    if "money market" in n:
        return "MM"
    return None


def _is_missing_dirname(name: str) -> bool:
    return name.strip().lower() == "missing"


def _is_status_dirname(name: str) -> str | None:
    for status in STATUS_DIRS:
        if name.strip().lower() == status.lower():
            return status
    return None


# ---------------------------------------------------------------------------
# Date parsing across every format observed in the wild
# ---------------------------------------------------------------------------

def _parse_date_from_parts(parts: list[str]) -> datetime | None:
    """
    Best-effort: scan a list of path components for a year, and a
    month/day combination in ANY of the formats this app (or its earlier,
    buggier versions) has produced, and return a single datetime.
    Returns None if no usable date could be assembled.
    """
    year = None
    month = None
    day = None

    for part in parts:
        m = _YEAR_RE.match(part)
        if m:
            year = int(m.group(1))

    if year is None:
        return None

    for part in parts:
        m = _DAY_MON_YEAR_RE.match(part)
        if m:
            d, mon_str, y = m.groups()
            mon = MONTH_ABBR_LOOKUP.get(mon_str[:3].lower())
            if mon:
                return datetime(int(y), mon, int(d))

    for part in parts:
        m = _MM_DASH_MONTH_RE.match(part)
        if m:
            _, mon_str = m.groups()
            month = MONTH_LOOKUP.get(mon_str.lower()) or MONTH_ABBR_LOOKUP.get(mon_str[:3].lower())

    if month is None:
        for part in parts:
            if part.lower() in MONTH_LOOKUP:
                month = MONTH_LOOKUP[part.lower()]

    for part in parts:
        m = _DAY_ONLY_RE.match(part)
        if m and 1 <= int(m.group(1)) <= 31:
            day = int(m.group(1))

    if month and day:
        try:
            return datetime(year, month, day)
        except ValueError:
            pass

    # "NN-NN-YYYY"-shaped folder — genuinely ambiguous (DD-MM vs MM-DD) in
    # isolation. If a sibling month-name folder already told us the month
    # (the common case here, e.g. ".../July/08-07-2026/..."), prefer
    # whichever of the two numbers matches that known month and treat the
    # other as the day — this app's date folders are DD-MM, not MM-DD.
    for part in parts:
        m = _MM_DD_YYYY_RE.match(part)
        if m:
            a, b, y = (int(x) for x in m.groups())
            if month is not None:
                if a == month:
                    day = b
                elif b == month:
                    day = a
                else:
                    day = a  # known month doesn't match either — keep DD-MM default, flagged by caller
                try:
                    return datetime(y, month, day)
                except ValueError:
                    continue
            # No independently-known month: assume DD-MM-YYYY (this app's
            # own convention) rather than MM-DD-YYYY.
            try:
                return datetime(y, b, a)
            except ValueError:
                continue

    if month:
        return datetime(year, month, 1)

    return datetime(year, 1, 1)


def _canonical_day_folder(dt: datetime) -> str:
    return dt.strftime("%d-%b-%Y")


def _canonical_month_folder(dt: datetime) -> str:
    return dt.strftime("%B")


def _canonical_year_folder(dt: datetime) -> str:
    return dt.strftime("%Y")


# ---------------------------------------------------------------------------
# Core reorganization
# ---------------------------------------------------------------------------

def _detect_category(rel_parts: list[str]) -> tuple[str, list[str], bool]:
    """
    Returns (category, remaining_parts, was_orphaned).
    A bare "2026/07-July/07/..." with no category prefix is orphaned data —
    matches the Disinvestments dated-folder shape, so it's merged in there,
    but was_orphaned=True so the caller can log it loudly.
    """
    if rel_parts and rel_parts[0] in FORM_CATEGORIES:
        return rel_parts[0], rel_parts[1:], False
    return "Disinvestments", rel_parts, True


def plan_moves(filed_root: Path) -> list[tuple[Path, Path, str]]:
    """
    Walks filed_root and returns a list of (source, destination, note)
    tuples describing every file move needed to reach the canonical
    structure. Does NOT touch the filesystem.
    """
    moves: list[tuple[Path, Path, str]] = []

    for source in sorted(filed_root.rglob("*")):
        if source.is_dir():
            continue

        rel_parts = list(source.relative_to(filed_root).parts[:-1])
        filename = source.name

        category, remainder, orphaned = _detect_category(rel_parts)
        if orphaned:
            logger.warning(
                "Orphaned/stray path with no category prefix: %s "
                "-> assuming '%s' (double-check this)",
                source, category,
            )

        is_missing_marker = any(_is_missing_dirname(p) for p in remainder)
        status = next((s for p in remainder if (s := _is_status_dirname(p))), None)
        bucket = next((b for p in remainder if (b := _is_money_market_dirname(p))), None)

        dt = _parse_date_from_parts(remainder)
        if dt is None:
            logger.warning(
                "Could not determine a date for %s from its path — "
                "falling back to file mtime.", source,
            )
            dt = datetime.fromtimestamp(source.stat().st_mtime)

        date_parts = [
            _canonical_year_folder(dt),
            _canonical_month_folder(dt),
            _canonical_day_folder(dt),
        ]

        if is_missing_marker:
            dest = filed_root / category / MISSING_DIR / filename
            note = "missing-marker -> consolidated top-level Missing/"
        elif status:
            # Status (Captured/Approved/Rejected) is always a FLAT sibling
            # of the date folder — never nested inside a fund bucket,
            # matching the reference diagram.
            dest = filed_root / category / Path(*date_parts) / status / filename
            note = f"status file -> flat {status}/ under date folder"
        elif bucket and category in FUND_BUCKET_CATEGORIES:
            bucket_name = MONEY_MARKET_CANONICAL if bucket == "MM" else NON_MONEY_MARKET_CANONICAL
            dest = filed_root / category / Path(*date_parts) / bucket_name / filename
            note = "raw extraction output -> canonical fund-bucket folder"
        else:
            # No status, no fund bucket recognised — file sat directly in a
            # date (or otherwise unrecognised) folder. Keep it at the date
            # level so nothing is silently dropped; flag for a human look.
            dest = filed_root / category / Path(*date_parts) / filename
            note = "no status/bucket folder recognised -> filed at date level (please review)"

        if dest != source:
            moves.append((source, dest, note))

    return moves


def apply_moves(moves: list[tuple[Path, Path, str]]) -> None:
    for source, dest, note in moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = dest
        counter = 1
        while target.exists():
            target = dest.parent / f"{dest.stem}_{counter}{dest.suffix}"
            counter += 1
        shutil.move(str(source), str(target))
        logger.info("Moved %s -> %s  [%s]", source, target, note)


def _prune_empty_dirs(root: Path) -> None:
    for dirpath in sorted(root.glob("**/*"), key=lambda p: len(p.parts), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()
            except OSError:
                pass  # not empty, leave it


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "filed_dir", type=Path,
        help="Path to the filed_documents root to reorganize (e.g. output/filed_documents).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually move files. Without this flag, only a dry-run report is printed.",
    )
    parser.add_argument(
        "--prune-empty", action="store_true",
        help="After applying, remove any directories left empty by the moves.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args.filed_dir.exists():
        raise SystemExit(f"No such directory: {args.filed_dir}")

    moves = plan_moves(args.filed_dir)

    if not moves:
        print("Nothing to do — tree already matches the canonical structure.")
        return

    counts: dict[str, int] = defaultdict(int)
    for _, _, note in moves:
        counts[note] += 1

    print(f"\n{len(moves)} file(s) need to move:")
    for note, n in counts.items():
        print(f"  {n:5d}  {note}")

    if not args.apply:
        print("\nDRY RUN — no files were moved. Re-run with --apply to execute.")
        print("First 20 planned moves:")
        for source, dest, note in moves[:20]:
            print(f"  {source}\n    -> {dest}")
        return

    apply_moves(moves)
    if args.prune_empty:
        _prune_empty_dirs(args.filed_dir)
    print(f"\nDone — moved {len(moves)} file(s).")


if __name__ == "__main__":
    main()