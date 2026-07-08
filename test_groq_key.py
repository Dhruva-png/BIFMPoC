# test_groq_key.py — run with: python test_groq_key.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

api_key = os.environ.get("GROQ_API_KEY", "")
print(f"Key loaded: {'yes, len=' + str(len(api_key)) if api_key else 'NO'}")

if not api_key:
    raise SystemExit("GROQ_API_KEY is empty — stop here, that's the bug.")

import requests

resp = requests.post(
    "https://api.groq.com/openai/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
        "max_tokens": 5,
    },
    timeout=30,
)
print(f"Status: {resp.status_code}")
print(resp.text[:500])