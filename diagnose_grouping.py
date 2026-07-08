"""
Run this from the project root:  python diagnose_grouping.py

Prints exactly which "app" package Python is loading, whether the fix is
actually present in it, and what it computes for the problem filename —
so we know for certain whether this is a file-replacement problem, a
different/shadow install of the app package, or something else entirely.
"""
import sys
from pathlib import Path

print("=" * 70)
print("1. Working directory:", Path.cwd())
print("=" * 70)

try:
    import app
    print("2. 'app' package is being loaded from:")
    print("   ", app.__file__)
except Exception as exc:
    print("2. FAILED to import 'app' at all:", exc)
    sys.exit(1)

print("=" * 70)

try:
    from app.core import pipeline
    print("3. 'app.core.pipeline' is being loaded from:")
    print("   ", pipeline.__file__)
except Exception as exc:
    print("3. FAILED to import 'app.core.pipeline':", exc)
    sys.exit(1)

print("=" * 70)

has_fix = hasattr(pipeline, "_strip_duplicate_suffix")
print("4. Does this loaded module have '_strip_duplicate_suffix'?", has_fix)

if not has_fix:
    print()
    print("   >>> THE FIX IS NOT IN THE FILE PYTHON IS ACTUALLY LOADING. <<<")
    print("   The file at the path printed in step 3 above still has the")
    print("   OLD code, regardless of what you replaced in File Explorer.")
    sys.exit(1)

print("=" * 70)

test_file = Path("ADD - GAOLATHE - Copy.pdf")
result = pipeline._person_key_for_file(test_file)
print(f"5. _person_key_for_file('{test_file.name}') returns: '{result}'")

if result == "GAOLATHE":
    print()
    print("   >>> THE FIX WORKS. If the app still misgroups this file, the")
    print("   Streamlit process itself is stale — it started before this")
    print("   file was updated and hasn't reloaded it. Fully stop Streamlit")
    print("   (Ctrl+C, confirm the prompt returns) and start it again.")
else:
    print()
    print(f"   >>> UNEXPECTED: the fix is present but returned '{result}'")
    print("   instead of 'GAOLATHE'. Send this whole output back and I'll dig in.")

print("=" * 70)