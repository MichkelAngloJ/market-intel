"""Source health: rules a-e, active problems and clearing, one email per rule window (the
alert throttle), and health's "sources" check. Fake history only."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from market_intel import health
from market_intel.alerts import AlertSender
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging
from market_intel.source_health import NEXT_STEP, SourceHealth, active_problems, state_path

T0 = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
SRC = "rss:fake_news"
FAKE_APP_PW = " ".join(["fakq", "wxyz", "abcd", "efgh"])  # built from parts: not a real value


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, level, title, body, **kwargs):
        self.calls.append({"level": level, "title": title, "body": body, **kwargs})

    @property
    def rules(self):
        return [c["context"]["rule"] for c in self.calls]


@pytest.fixture
def tracker(tmp_path):
    alerts = Recorder()
    return SourceHealth(tmp_path, now=lambda: T0, alert=alerts), alerts


def test_three_failures_in_a_row_alert_once_and_a_success_clears(tracker):
    sh, alerts = tracker
    assert sh.record_failure(SRC, "HTTP 503") == []
    assert sh.record_failure(SRC, "HTTP 503") == []
    assert sh.record_failure(SRC, "HTTP 503") == ["failures"]
    [call] = alerts.calls
    assert call["level"] == "warning" and call["key"] == f"source_health:{SRC}:failures"
    assert SRC in call["title"] and "3 consecutive failures" in call["body"]
    assert "HTTP 503" in call["body"] and NEXT_STEP in call["body"]
    assert "failures" in sh.state()[SRC]["problems"]
    sh.record_not_modified(SRC)
    assert sh.state()[SRC]["problems"] == {} and sh.state()[SRC]["consecutive_failures"] == 0


def test_the_stored_error_is_masked(tracker):
    sh, _ = tracker
    fake_link = "https://" + "someone" + ":" + "not-a-real-pw-1" + "@feed.invalid/x"
    sh.record_failure(SRC, f"ConnectError: {fake_link}")
    text = state_path(sh.path.parent.parent).read_text(encoding="utf-8")
    assert "someone" not in text and "not-a-real-pw-1" not in text and "***@feed.invalid" in text


def test_b_content_but_nothing_parsed(tracker):
    sh, alerts = tracker
    assert sh.record_content(SRC, items=0, malformed=0, without_time=0, recognised=False) == [
        "empty"
    ]
    assert sh.record_content(SRC, items=0, malformed=4, without_time=0) == ["empty", "malformed"]
    assert sh.record_content(SRC, items=0, malformed=0, without_time=0) == []  # just empty
    assert "empty" not in sh.state()[SRC]["problems"]


def test_c_mostly_malformed(tracker):
    sh, _ = tracker
    assert sh.record_content(SRC, items=4, malformed=6, without_time=0) == ["malformed"]
    assert sh.record_content(SRC, items=5, malformed=5, without_time=0) == []  # 50% is fine
    assert sh.state()[SRC]["problems"] == {}


def test_d_volume_drop_needs_five_fetches_of_history(tracker):
    sh, alerts = tracker
    for _ in range(4):
        sh.record_content(SRC, items=50, malformed=0, without_time=0)
    assert sh.record_content(SRC, items=1, malformed=0, without_time=0) == []  # only 4 known
    for _ in range(5):
        sh.record_content(SRC, items=50, malformed=0, without_time=0)
    assert sh.record_content(SRC, items=9, malformed=0, without_time=0) == ["volume_drop"]
    assert "9 items, usual median 50" in alerts.calls[-1]["body"]
    assert sh.record_content(SRC, items=10, malformed=0, without_time=0) == []  # 20% is fine
    assert len(sh.state()[SRC]["counts"]) <= 20


def test_d_ignores_a_feed_that_is_usually_empty(tracker):
    sh, _ = tracker
    for _ in range(6):
        sh.record_content(SRC, items=0, malformed=0, without_time=0)
    assert sh.record_content(SRC, items=0, malformed=0, without_time=0) == []


def test_e_publish_times_missing(tracker):
    sh, _ = tracker
    assert sh.record_content(SRC, items=10, malformed=0, without_time=6) == ["no_publish_time"]
    assert sh.record_content(SRC, items=10, malformed=0, without_time=5) == []


def test_problems_are_kept_per_source_and_filtered(tracker, tmp_path):
    sh, _ = tracker
    sh.record_content("rss:a", items=1, malformed=9, without_time=0)
    sh.record_content("rss:b", items=10, malformed=0, without_time=0)
    assert set(active_problems(tmp_path)) == {"rss:a"}
    assert active_problems(tmp_path, ["rss:b"]) == {}


# --- the throttle: one email per source+rule window ---------------------------------------


def alert_settings(tmp_path, monkeypatch):
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "ALERT_EMAIL_FROM": "sender@example.invalid",
        "ALERT_EMAIL_TO": "owner@example.invalid",
        "ALERT_EMAIL_APP_PASSWORD": FAKE_APP_PW,
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, console_stream=io.StringIO())
    return settings


def test_repeated_problems_send_one_email_per_window(tmp_path, monkeypatch):
    settings = alert_settings(tmp_path, monkeypatch)
    clock = [T0]
    sender = AlertSender(settings, clock=lambda: clock[0])
    delivered = []
    monkeypatch.setattr(sender, "_deliver_with_retry", lambda message: delivered.append(message))
    statuses = []

    def alert(level, title, body, **kw):
        statuses.append(sender.send(level, title, body, dry_run=False, **kw).status)

    sh = SourceHealth(settings.data_dir, now=lambda: clock[0], alert=alert)
    for _ in range(5):
        sh.record_failure(SRC, "HTTP 503")
    sh.record_content("rss:other", items=0, malformed=0, without_time=0, recognised=False)
    assert statuses == ["sent", "suppressed", "suppressed", "sent"]  # other source/rule: own key
    assert len(delivered) == 2
    clock[0] = T0 + timedelta(hours=settings.alert_throttle_hours, minutes=1)
    sh.record_failure(SRC, "HTTP 503")
    assert statuses[-1] == "sent"


# --- health's "sources" check -------------------------------------------------------------


def health_ctx(tmp_path, monkeypatch, feeds_toml: str):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    path = tmp_path / "feeds.toml"
    path.write_text(feeds_toml, encoding="utf-8")
    monkeypatch.setenv("NEWS_FEEDS_FILE", str(path))
    settings = load_settings()
    return health.Context(lambda: settings), settings


FEEDS = (
    '[[feed]]\nname = "fake_news"\nurl = "https://x.invalid/a"\nkind = "news"\npublisher = "P"\n'
    '[[feed]]\nname = "off"\nurl = "https://x.invalid/b"\nkind = "news"\npublisher = "P"\n'
    "enabled = false\n"
)


def test_sources_check_skip_ok_warn(tmp_path, monkeypatch):
    ctx, settings = health_ctx(tmp_path, monkeypatch, FEEDS)
    assert health.check_sources(ctx).status == "SKIP"
    sh = SourceHealth(settings.data_dir, now=lambda: T0, alert=Recorder())
    sh.record_content(SRC, items=10, malformed=0, without_time=0)
    ok = health.check_sources(ctx)
    assert (ok.status, ok.message) == ("OK", "1 sources, no active problems")
    sh.record_content("rss:off", items=0, malformed=5, without_time=0)  # disabled: ignored
    assert health.check_sources(ctx).status == "OK"
    for _ in range(3):
        sh.record_failure(SRC, "HTTP 403")
    warn = health.check_sources(ctx)
    assert warn.status == "WARN" and "rss:fake_news: failures" in warn.message
    assert json.dumps(warn.details)  # JSON-friendly for health --json
