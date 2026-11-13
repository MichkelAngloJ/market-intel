"""Tests for alerts: levels, email content, throttle, failures, masking. No real SMTP."""

import io
import smtplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_intel import alerts
from market_intel.alerts import FOOTER, AlertSender
from market_intel.config import Settings, load_settings
from market_intel.logging_setup import LOG_FILE_NAME, configure_logging

FAKE = "fake-secret-value-123"
FAKE_APP_PW = "fakq wxyz abcd efgh"  # pragma: allowlist secret
SENDER = "alerts.sender@example.com"
RECIPIENT = "me.main@example.com"
NOT_LOADED = "plain-value-under-sensitive-name"
T0 = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


class FakeSMTP:
    """Records what the alerts code does with an SMTP connection."""

    instances: list["FakeSMTP"] = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.login_args = (user, password)
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        self.sent.append(msg)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    base = {
        "APP_PROFILE": "hp",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "ALERT_EMAIL_FROM": SENDER,
        "ALERT_EMAIL_TO": RECIPIENT,
        "ALERT_EMAIL_APP_PASSWORD": FAKE_APP_PW,
    }
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, console_stream=io.StringIO())
    return settings


def sender(settings: Settings, clock: Clock | None = None, sleeps: list | None = None):
    return AlertSender(
        settings,
        clock=clock or Clock(T0),
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def log_text(settings: Settings) -> str:
    return (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


# --- levels and content ------------------------------------------------------------------


def test_info_is_logged_not_emailed(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    result = sender(settings).send("info", "Heads up", "fyi")
    assert result.status == "logged"
    assert fake_smtp.instances == []
    assert '"status": "logged"' in log_text(settings)


def test_warning_email_content_dry_run(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    result = sender(settings).send(
        "warning", "Disk low", "Only 2 GB left.", context={"drive": "C:"}, dry_run=True
    )
    assert result.status == "dry_run"
    assert result.recipient == "m***@example.com"
    msg = result.message
    assert msg["Subject"] == "[market-intel][WARNING] Disk low"
    assert msg["To"] == RECIPIENT and msg["From"] == SENDER
    body = msg.get_content()
    for part in (
        "Disk low",
        "Level:   WARNING",
        "2026-11-12 06:00:00 UTC",
        "2026-11-12 11:30:00 Asia/Kolkata",
        "Profile: hp",
        "Only 2 GB left.",
        "drive: C:",
        FOOTER,
    ):
        assert part in body


def test_dry_run_setting_sends_nothing(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch, ALERT_DRY_RUN="true")
    assert sender(settings).send("critical", "X", "y").status == "dry_run"
    assert fake_smtp.instances == []


def test_warning_sent_with_starttls_and_spaceless_password(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    result = sender(settings).send("warning", "Token expiring", "Re-login.")
    assert result.status == "sent"
    [conn] = fake_smtp.instances
    assert (conn.host, conn.port, conn.timeout) == ("smtp.gmail.com", 587, 20)
    assert conn.calls == ["starttls"]
    assert conn.login_args == (SENDER, FAKE_APP_PW.replace(" ", ""))
    assert conn.sent[0]["Subject"] == "[market-intel][WARNING] Token expiring"
    assert "m***@example.com" in log_text(settings)
    assert RECIPIENT not in log_text(settings)


def test_port_465_uses_ssl_without_starttls(tmp_path, monkeypatch, fake_smtp):
    calls = []

    class FakeSSL(FakeSMTP):
        def __init__(self, *args, **kwargs):
            calls.append("ssl")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    settings = make_settings(tmp_path, monkeypatch, ALERT_SMTP_PORT="465")
    assert sender(settings).send("critical", "X", "y").status == "sent"
    assert calls == ["ssl"]
    assert fake_smtp.instances[0].calls == []


def test_missing_alert_settings_fails_without_raising(tmp_path, monkeypatch, fake_smtp):
    monkeypatch.setenv("APP_PROFILE", "hp")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    result = AlertSender(load_settings()).send("warning", "X", "y")
    assert result.status == "failed"
    assert "ALERT_EMAIL_TO" in result.error
    assert fake_smtp.instances == []


# --- throttle ----------------------------------------------------------------------------


def test_throttle_window(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    clock = Clock(T0)
    s = sender(settings, clock)
    assert s.send("warning", "Feed down", "a").status == "sent"
    clock.now = T0 + timedelta(hours=1)
    assert s.send("warning", "Feed down", "b").status == "suppressed"
    assert s.send("warning", "Other problem", "c").status == "sent"  # different key
    assert s.send("critical", "Feed down", "d").status == "sent"  # level is part of the key
    clock.now = T0 + timedelta(hours=6, seconds=1)
    assert s.send("warning", "Feed down", "e").status == "sent"
    assert len(fake_smtp.instances) == 4
    assert '"status": "suppressed"' in log_text(settings)  # suppressed is still logged


def test_explicit_key_groups_alerts(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    s = sender(settings)
    assert s.send("warning", "Feed down: NSE", "a", key="feed").status == "sent"
    assert s.send("warning", "Feed down: BSE", "b", key="feed").status == "suppressed"


def test_throttle_survives_restart(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    assert sender(settings, Clock(T0)).send("warning", "Feed down", "a").status == "sent"
    state = settings.data_dir / "state" / "alert_throttle.json"
    assert state.is_file()
    restarted = sender(settings, Clock(T0 + timedelta(hours=2)))
    assert restarted.send("warning", "Feed down", "b").status == "suppressed"


def test_throttle_zero_hours_disables_it(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch, ALERT_THROTTLE_HOURS="0")
    s = sender(settings)
    assert s.send("warning", "X", "a").status == "sent"
    assert s.send("warning", "X", "b").status == "sent"


def test_failed_send_is_not_recorded_in_throttle(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    fake_smtp.fail_login = True
    assert sender(settings).send("warning", "X", "a").status == "failed"
    fake_smtp.fail_login = False
    assert sender(settings).send("warning", "X", "b").status == "sent"


# --- failures and masking ----------------------------------------------------------------


def test_auth_failure_retries_once_then_fails_quietly(tmp_path, monkeypatch, fake_smtp):
    settings = make_settings(tmp_path, monkeypatch)
    fake_smtp.fail_login = True
    sleeps: list[float] = []
    result = sender(settings, sleeps=sleeps).send("critical", "DB down", "x")
    assert result.status == "failed"
    assert "SMTPAuthenticationError" in result.error
    assert sleeps == [5]
    assert len(fake_smtp.instances) == 2
    text = log_text(settings)
    assert '"status": "failed"' in text
    assert FAKE_APP_PW not in text and FAKE_APP_PW.replace(" ", "") not in text


def test_subject_body_and_context_are_masked(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, GEMINI_API_KEY=FAKE)
    result = sender(settings).send(
        "warning",
        f"Cannot reach postgresql://u:{FAKE}@db.invalid/x",
        f"key was {FAKE} and pw {FAKE_APP_PW}",
        context={"api_key": NOT_LOADED, "note": f"x{FAKE}"},
        dry_run=True,
    )
    msg = result.message
    whole = msg["Subject"] + msg.get_content()
    assert "postgresql://u:***@db.invalid" in msg["Subject"]
    for leaked in (FAKE, FAKE_APP_PW, NOT_LOADED):
        assert leaked not in whole
    assert FAKE not in log_text(settings)


def test_title_newlines_cannot_inject_headers(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    result = sender(settings).send("warning", "Hi\nBcc: x@evil.invalid", "b", dry_run=True)
    assert result.message["Bcc"] is None
    assert "\n" not in result.message["Subject"]


# --- command -----------------------------------------------------------------------------


def test_command_dry_run(tmp_path, monkeypatch, capsys, fake_smtp):
    make_settings(tmp_path, monkeypatch)
    assert alerts.main(["--test", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "status: dry_run" in out
    assert "recipient: m***@example.com" in out
    assert RECIPIENT not in out
    assert fake_smtp.instances == []


def test_command_test_bypasses_throttle(tmp_path, monkeypatch, capsys, fake_smtp):
    make_settings(tmp_path, monkeypatch)
    assert alerts.main(["--test"]) == 0
    assert alerts.main(["--test"]) == 0
    assert capsys.readouterr().out.count("status: sent") == 2
