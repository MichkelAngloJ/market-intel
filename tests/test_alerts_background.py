"""Task 1.0: alerts never block a job, queued alerts are flushed at exit (bounded), and two
programs alerting at the same moment send at most one email per throttle window."""

import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_intel import alerts
from market_intel.alerts import AlertSender, BackgroundAlerts, ThrottleStore
from market_intel.config import load_settings
from market_intel.file_lock import file_lock
from market_intel.logging_setup import LOG_FILE_NAME, configure_logging

SENDER = "alerts.sender@example.com"
RECIPIENT = "me.main@example.com"
FAKE_APP_PW = "fakq wxyz abcd efgh"  # pragma: allowlist secret
T0 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)


def alert_env(tmp_path: Path) -> dict[str, str]:
    return {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "ALERT_EMAIL_FROM": SENDER,
        "ALERT_EMAIL_TO": RECIPIENT,
        "ALERT_EMAIL_APP_PASSWORD": FAKE_APP_PW,
    }


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in alert_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    s = load_settings()
    configure_logging(s, console_stream=io.StringIO())
    return s


class BlockingSender:
    """Stands in for AlertSender: each send waits until released."""

    def __init__(self):
        self.release = threading.Event()
        self.sent = []

    def send(self, level, title, body, **kwargs):
        self.release.wait(10)
        self.sent.append((level, title))


# --- background queue ------------------------------------------------------------------------


def test_send_returns_at_once_and_flush_waits():
    fake = BlockingSender()
    bg = BackgroundAlerts(fake, register_atexit=False)
    start = time.monotonic()
    for i in range(5):
        bg.send("critical", f"job failed {i}", "body")
    assert time.monotonic() - start < 0.5  # nothing waited for the "SMTP" call
    assert bg.pending == 5 and fake.sent == []
    fake.release.set()
    assert bg.flush(5) is True
    assert bg.pending == 0 and len(fake.sent) == 5


def test_flush_wait_is_bounded(settings):
    fake = BlockingSender()
    bg = BackgroundAlerts(fake, register_atexit=False)
    bg.send("critical", "stuck", "body")
    start = time.monotonic()
    assert bg.flush(0.3) is False
    assert 0.25 <= time.monotonic() - start < 2
    fake.release.set()
    logs = (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "alerts_not_flushed" in logs


def test_queued_dry_run_behaves_like_a_direct_one(settings):
    bg = BackgroundAlerts(AlertSender(settings, clock=lambda: T0), register_atexit=False)
    bg.send("warning", "Disk low", "Only 2 GB left.", dry_run=True)
    assert bg.flush(5)
    lines = [json.loads(x) for x in (settings.log_dir / LOG_FILE_NAME).read_text().splitlines()]
    [alert] = [r for r in lines if r["event"] == "alert"]
    assert alert["status"] == "dry_run" and alert["recipient"] == "m***@example.com"
    assert ThrottleStore(settings.data_dir / "state" / "alert_throttle.json").load() == {}


def test_queue_alert_uses_one_process_wide_sender(settings, monkeypatch):
    monkeypatch.setattr(alerts, "_background", None)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    monkeypatch.setattr(alerts.atexit, "register", lambda fn: fn)
    alerts.queue_alert("info", "hello", "body")
    first = alerts.background_alerts()
    assert alerts.background_alerts() is first and first.flush(5)


# --- at exit (real processes) ------------------------------------------------------------------

EXIT_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from market_intel import alerts
    from market_intel.config import load_settings
    from market_intel.logging_setup import configure_logging

    out, delay, timeout = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])

    def deliver(self, message):
        time.sleep(delay)
        out.write_text(message["Subject"])

    alerts.AlertSender._deliver = deliver
    settings = load_settings()
    configure_logging(settings, "exit_test")
    bg = alerts.BackgroundAlerts(alerts.AlertSender(settings), flush_timeout_s=timeout)
    bg.send("critical", "Job failed", "details")
    print("queued", flush=True)
    # no explicit flush: the atexit hook must do it
    """
)


def child_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    env.update(alert_env(tmp_path), MARKET_INTEL_ENV_FILE=str(empty), PYTHONIOENCODING="utf-8")
    return env


def run_child(tmp_path, delay, timeout):
    out = tmp_path / "sent.txt"
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", EXIT_CHILD, str(out), str(delay), str(timeout)],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("queued")
    return out, time.monotonic() - start


def test_queued_alert_is_flushed_at_exit(tmp_path):
    out, _ = run_child(tmp_path, delay=0.5, timeout=10)
    assert out.read_text() == "[market-intel][CRITICAL] Job failed"


def test_exit_waits_no_longer_than_the_flush_timeout(tmp_path):
    out, elapsed = run_child(tmp_path, delay=30, timeout=1)
    assert not out.exists()  # the stuck send was abandoned...
    assert elapsed < 20  # ...after the bounded wait, not after 30 s
    logs = (tmp_path / "logs" / "exit_test.jsonl").read_text(encoding="utf-8")
    assert "alerts_not_flushed" in logs


# --- two processes, one throttle window ---------------------------------------------------------

RACE_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from market_intel import alerts
    from market_intel.config import load_settings
    from market_intel.logging_setup import configure_logging

    name, go, sent = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

    def deliver(self, message):
        with sent.open("a", encoding="utf-8") as fh:  # one line per "email"
            fh.write(name + "\\n")
        time.sleep(0.3)  # a slow SMTP server widens the race

    alerts.AlertSender._deliver = deliver
    settings = load_settings()
    configure_logging(settings, name)
    sender = alerts.AlertSender(settings)
    Path(str(go) + "." + name).write_text("ready")
    while not go.exists():
        time.sleep(0.005)
    print(sender.send("critical", "Disk almost full", "same alert from every program").status)
    """
)


def test_two_processes_alerting_at_once_send_one_email(tmp_path):
    go, sent = tmp_path / "go", tmp_path / "sent.log"
    names = [f"race_{i}" for i in range(4)]
    env = child_env(tmp_path)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", RACE_CHILD, name, str(go), str(sent)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in names
    ]
    deadline = time.monotonic() + 60
    while not all(Path(f"{go}.{n}").exists() for n in names):
        assert time.monotonic() < deadline, "children never got ready"
        assert all(c.poll() is None for c in children), "a child exited early"
        time.sleep(0.02)
    go.write_text("go")
    statuses = []
    for child in children:
        out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
        statuses.append(out.strip())
    assert sorted(statuses) == ["sent", "suppressed", "suppressed", "suppressed"]
    assert len(sent.read_text(encoding="utf-8").splitlines()) == 1


# --- the claim is given back when sending fails ---------------------------------------------------


def test_failed_send_gives_the_claim_back_and_keeps_an_older_entry(settings, monkeypatch):
    store = ThrottleStore(settings.data_dir / "state" / "alert_throttle.json")
    older = datetime(2026, 1, 1, tzinfo=UTC)
    store.record("critical:X", older)

    def boom(self, message):
        raise OSError("smtp down")

    monkeypatch.setattr(AlertSender, "_deliver", boom)
    monkeypatch.setenv("ALERT_THROTTLE_HOURS", "1")
    s = load_settings()
    result = AlertSender(s, clock=lambda: T0, sleep=lambda _s: None).send("critical", "X", "y")
    assert result.status == "failed"
    assert store.load() == {"critical:X": older.isoformat()}


# --- the file lock itself ------------------------------------------------------------------------


def test_file_lock_excludes_a_second_holder_and_times_out(tmp_path):
    path = tmp_path / "x.lock"
    with file_lock(path):
        with pytest.raises(TimeoutError, match="x.lock"):
            with file_lock(path, timeout_s=0.1):
                pass
    with file_lock(path, timeout_s=0.1):  # free again
        pass
