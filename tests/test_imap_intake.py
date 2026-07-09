import imaplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import imap_client
from config.settings import settings


def _build_email_bytes(*, sender: str, when: datetime | None, with_pdf: bool = True) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = "Instruction form"
    msg["Message-ID"] = "<test@example.com>"
    if when is not None:
        msg["Date"] = format_datetime(when)
    msg.set_content("body")
    if with_pdf:
        msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="form.pdf")
    return msg.as_bytes()


class FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4_SSL - just enough surface for
    fetch_pdf_attachments to run against a fixed set of in-memory messages."""

    def __init__(self, messages: dict[bytes, bytes]):
        self._messages = messages
        self.seen: list[bytes] = []

    def login(self, *_args, **_kwargs):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "OK", [b""]

    def search(self, _charset, _query):
        return "OK", [b" ".join(self._messages.keys())]

    def fetch(self, num, _spec):
        return "OK", [(num, self._messages[num])]

    def store(self, num, _flag_op, flags):
        if flags == "\\Seen":
            self.seen.append(num)
        return "OK", []

    def copy(self, *_args, **_kwargs):
        return "OK", []

    def expunge(self):
        return "OK", []

    def logout(self):
        return "OK", []


def _configure_imap(
    monkeypatch, messages: dict[bytes, bytes],
    min_unread_minutes: int = 60, allowed_senders: str = "",
):
    monkeypatch.setattr(settings.imap, "host", "mail.example.com")
    monkeypatch.setattr(settings.imap, "username", "intake@example.com")
    monkeypatch.setattr(settings.imap, "password", "secret")
    monkeypatch.setattr(settings.imap, "processed_folder", "")  # skip the move-to-folder path
    monkeypatch.setattr(settings.imap, "min_unread_minutes", min_unread_minutes)
    # Default blank (no restriction) so age-focused tests aren't coupled to
    # the sender allowlist - see the dedicated allowed-senders tests below.
    monkeypatch.setattr(settings.imap, "allowed_senders", allowed_senders)
    fake_conn = FakeIMAP(messages)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port: fake_conn)
    return fake_conn


def test_message_newer_than_threshold_is_not_processed(tmp_path, monkeypatch):
    # Arrived 5 minutes ago - well under the 60-minute minimum unread age.
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    messages = {b"1": _build_email_bytes(sender="client@example.com", when=fresh)}
    _configure_imap(monkeypatch, messages)

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert results == []
    assert list(tmp_path.glob("*.pdf")) == []


def test_message_older_than_threshold_is_processed(tmp_path, monkeypatch):
    # Sitting unread for 2 hours - past the 60-minute minimum, must be picked up.
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {b"1": _build_email_bytes(sender="client@example.com", when=old)}
    conn = _configure_imap(monkeypatch, messages)

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1
    assert results[0]["sender"] == "client@example.com"
    assert results[0]["path"].exists()
    assert b"1" in conn.seen  # marked \Seen so it isn't reprocessed


def test_message_with_no_date_header_is_skipped_not_processed(tmp_path, monkeypatch):
    # Can't verify age at all - must be held back, not processed by default.
    messages = {b"1": _build_email_bytes(sender="client@example.com", when=None)}
    _configure_imap(monkeypatch, messages)

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert results == []


def test_blank_allowed_senders_disables_the_check(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {b"1": _build_email_bytes(sender="random.client@somewhere.co.bw", when=old)}
    _configure_imap(monkeypatch, messages, allowed_senders="")

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1
    assert results[0]["sender"] == "random.client@somewhere.co.bw"


def test_sender_not_in_allowlist_is_skipped(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {b"1": _build_email_bytes(sender="stranger@somewhere.co.bw", when=old)}
    _configure_imap(monkeypatch, messages, allowed_senders="shyam.sp@kgisl.com")

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert results == []


def test_sender_in_allowlist_is_processed_even_with_display_name(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {
        b"1": _build_email_bytes(sender="Shyam S P <shyam.sp@kgisl.com>", when=old),
    }
    _configure_imap(monkeypatch, messages, allowed_senders="shyam.sp@kgisl.com")

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1


def test_allowlist_match_is_case_insensitive(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {b"1": _build_email_bytes(sender="Shyam.SP@KGISL.com", when=old)}
    _configure_imap(monkeypatch, messages, allowed_senders="shyam.sp@kgisl.com")

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1


def test_allowlist_supports_multiple_comma_separated_addresses(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    messages = {
        b"1": _build_email_bytes(sender="branch.a@bifm.co.bw", when=old),
        b"2": _build_email_bytes(sender="not.allowed@example.com", when=old),
    }
    _configure_imap(
        monkeypatch, messages,
        allowed_senders="shyam.sp@kgisl.com, branch.a@bifm.co.bw",
    )

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1
    assert results[0]["sender"] == "branch.a@bifm.co.bw"


def test_default_allowed_senders_restores_previous_test_address():
    # config.settings.ImapSettings.allowed_senders defaults to the
    # previously-hardcoded address, so intake is restricted out of the box
    # unless IMAP_ALLOWED_SENDERS is set otherwise.
    assert "shyam.sp@kgisl.com" in imap_client._allowed_sender_set()


def test_custom_min_unread_minutes_is_respected(tmp_path, monkeypatch):
    # 10 minutes old, with a 5-minute threshold - should be processed.
    ten_min_old = datetime.now(timezone.utc) - timedelta(minutes=10)
    messages = {b"1": _build_email_bytes(sender="client@example.com", when=ten_min_old)}
    _configure_imap(monkeypatch, messages, min_unread_minutes=5)

    results = imap_client.fetch_pdf_attachments(tmp_path)

    assert len(results) == 1


if __name__ == "__main__":
    print("Run via pytest - this module uses the monkeypatch/tmp_path fixtures.")
