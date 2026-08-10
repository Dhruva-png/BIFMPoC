"""
Tests for the two-key round-robin / cross-key-failover support in
app.llm.gemini_client: config.settings.GeminiSettings.api_keys, the
round-robin key selector, and _generate()/check_connection() actually
routing around a rate-limited or dead key instead of always using the
first one. The real google-genai SDK client is replaced with a fake so
these run with no network access.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm.gemini_client as gc
from config.settings import GeminiSettings
from google.genai import errors as genai_errors


def test_api_keys_property_skips_unset():
    assert GeminiSettings(api_key="", api_key_2="").api_keys == []
    assert GeminiSettings(api_key="only-one", api_key_2="").api_keys == ["only-one"]
    assert GeminiSettings(api_key="first", api_key_2="second").api_keys == ["first", "second"]


def test_next_key_round_robins_across_the_pool():
    keys = ["A", "B"]
    picks = [gc._next_key(keys) for _ in range(6)]
    # The shared counter's starting phase isn't deterministic across the
    # test session, but every consecutive pair must differ, and A/B must
    # appear equally often over an even number of picks.
    assert all(picks[i] != picks[i + 1] for i in range(len(picks) - 1))
    assert picks.count("A") == 3 and picks.count("B") == 3


class _FakeClient:
    """Stands in for google.genai.Client - the key that was rate-limited
    always 429s, any other key always succeeds, so the test is immune to
    which key the round-robin happens to try first."""
    RATE_LIMITED_KEY = "rate-limited-key"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.models = self

    def generate_content(self, model, contents, config):
        if self.api_key == self.RATE_LIMITED_KEY:
            raise genai_errors.ClientError(429, {"error": {"message": "rate limited"}})
        return SimpleNamespace(text="OK", model_dump=lambda exclude_none=True: {})


def _reset_client_cache(monkeypatch):
    monkeypatch.setattr(gc, "_client_cache", {})
    monkeypatch.setattr(gc, "genai", SimpleNamespace(Client=_FakeClient))


def test_generate_rotates_off_a_rate_limited_key_onto_a_working_one(monkeypatch):
    _reset_client_cache(monkeypatch)
    monkeypatch.setattr(gc.settings.gemini, "api_key", _FakeClient.RATE_LIMITED_KEY)
    monkeypatch.setattr(gc.settings.gemini, "api_key_2", "healthy-key")

    response, elapsed = gc._generate("gemini-3.1-flash-lite", "prompt", {})
    assert response.text == "OK"


def test_generate_fails_when_every_key_is_rate_limited(monkeypatch):
    _reset_client_cache(monkeypatch)
    monkeypatch.setattr(gc.settings.gemini, "api_key", _FakeClient.RATE_LIMITED_KEY)
    monkeypatch.setattr(gc.settings.gemini, "api_key_2", "")
    monkeypatch.setattr(gc.settings.gemini, "max_retries", 0)  # skip the real sleep-and-retry path
    monkeypatch.setattr(gc.time, "sleep", lambda *a, **kw: None)

    try:
        gc._generate("gemini-3.1-flash-lite", "prompt", {})
        assert False, "expected GeminiError"
    except gc.GeminiError:
        pass


def test_check_connection_true_if_any_configured_key_works(monkeypatch):
    _reset_client_cache(monkeypatch)
    monkeypatch.setattr(gc.settings.gemini, "api_key", _FakeClient.RATE_LIMITED_KEY)
    monkeypatch.setattr(gc.settings.gemini, "api_key_2", "healthy-key")

    assert gc.check_connection() is True


def test_check_connection_false_with_no_keys_configured(monkeypatch):
    monkeypatch.setattr(gc.settings.gemini, "api_key", "")
    monkeypatch.setattr(gc.settings.gemini, "api_key_2", "")

    assert gc.check_connection() is False
