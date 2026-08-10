# diagnose_env.py — run with: python diagnose_env.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

print(f"Looking for .env at: {env_path}")
print(f"Exists: {env_path.exists()}")

if env_path.exists():
    print("\n--- Raw .env contents (values masked) ---")
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            print(f"  {key} = <{len(line.split('=', 1)[1].strip())} chars>")
        elif line.strip():
            print(f"  (skipped/comment): {line}")

print("\n--- Already set in real OS environment BEFORE .env loads ---")
for key in ("GEMINI_API_KEY", "GEMINI_API_KEY_2"):
    pre_existing = os.environ.get(key)
    print(f"  {key}: {'SET (len=' + str(len(pre_existing)) + ')' if pre_existing else 'not set'}")

print("\n--- Loading .env now ---")
from dotenv import load_dotenv
load_dotenv(env_path, override=False)

print("\n--- Final resolved values ---")
for key in ("GEMINI_API_KEY", "GEMINI_API_KEY_2"):
    val = os.environ.get(key, "")
    print(f"  {key}: {'SET (len=' + str(len(val)) + ')' if val else 'EMPTY'}")

n_keys = sum(1 for k in ("GEMINI_API_KEY", "GEMINI_API_KEY_2") if os.environ.get(k))
print(f"\n{n_keys} Gemini key(s) configured "
      f"{'- requests will round-robin across both' if n_keys == 2 else '(single-key mode)' if n_keys == 1 else '- app.llm calls will fail until at least one is set'}.")