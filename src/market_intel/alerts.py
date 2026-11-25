"""Alerts: log every alert, email warning/critical ones, with a throttle and a dry-run mode.

    send_alert("warning", "Upstox token expires soon", "Re-login before 06:00 IST.")

- info      -> logged only
- warning   -> logged and emailed
- critical  -> logged and emailed

The same alert (key, default "level:title") is emailed at most once per ALERT_THROTTLE_HOURS.
Throttle state lives in DATA_DIR/state/alert_throttle.json so it survives restarts. Reading and
updating it happens under a cross-process file lock (alert_throttle.json.lock), and the window
is claimed BEFORE the email goes out (and given back if sending fails), so two programs
alerting at the same moment send at most one email per window.
Sending never raises: failures are logged and returned as status "failed".

Jobs don't wait for the email: ``queue_alert(...)`` (or a ``BackgroundAlerts``) hands the alert
to a background thread and returns at once. At exit, queued alerts are flushed with a bounded
wait (FLUSH_TIMEOUT_S); whatever is still queued then is logged as not sent.

Manual check: ``uv run python -m market_intel.alerts --test [--dry-run]``.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import smtplib
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from market_intel.config import ConfigError, Settings, get_settings, mask_email
from market_intel.file_lock import file_lock, safe_replace
from market_intel.logging_setup import configure_logging, get_logger, mask_text, mask_value

Level = Literal["info", "warning", "critical"]
Status = Literal["sent", "logged", "suppressed", "dry_run", "failed"]

SMTP_TIMEOUT_S = 20
RETRY_DELAY_S = 5
SSL_PORT = 465
FLUSH_TIMEOUT_S = 60.0  # at exit: wait at most this long for queued alerts
FOOTER = "Automated alert from market-intel. Not investment advice."

log = get_logger("market_intel.alerts")  # fixed name: __name__ is "__main__" under -m


@dataclass(frozen=True)
class AlertResult:
    """Outcome of one alert. ``recipient`` is already masked."""

    status: Status
    throttle_id: str
    recipient: str | None = None
    error: str | None = None
    message: EmailMessage | None = field(default=None, repr=False)


class ThrottleStore:
    """Last-sent time per alert key, kept in a JSON file written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")

    def locked(self):
        """Cross-process lock for a read-modify-write of the state file."""
        return file_lock(self.lock_path)

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("alert_throttle_state_unreadable", path=str(self.path))
            return {}
        return data if isinstance(data, dict) else {}

    def last_sent(self, key: str) -> datetime | None:
        value = self.load().get(key)
        return datetime.fromisoformat(value) if value else None

    def record(self, key: str, when: datetime) -> None:
        data = self.load()
        data[key] = when.isoformat()
        self._write(data)

    def restore(self, key: str, claimed: datetime, previous: str | None) -> None:
        """Undo a claim made at ``claimed`` (only if nobody has claimed the key since)."""
        data = self.load()
        if data.get(key) != claimed.isoformat():
            return
        if previous is None:
            data.pop(key)
        else:
            data[key] = previous
        self._write(data)

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same folder, then swap it in: a crash can't leave
        # a half-written file behind.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".throttle-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            safe_replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class AlertSender:
    """Builds, throttles and sends alerts. Clock and sleep are injectable for tests."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        state_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.throttle = ThrottleStore(
            state_path or settings.data_dir / "state" / "alert_throttle.json"
        )

    def send(
        self,
        level: Level,
        title: str,
        body: str,
        *,
        key: str | None = None,
        context: Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
        use_throttle: bool = True,
    ) -> AlertResult:
        """Log the alert and, for warning/critical, email it. Never raises."""
        try:
            return self._send(level, title, body, key, context or {}, dry_run, use_throttle)
        except Exception as exc:  # last resort: alerts must never crash the caller
            log.error("alert_internal_error", error_type=type(exc).__name__, error=str(exc))
            return AlertResult(status="failed", throttle_id=key or f"{level}:{title}")

    def _send(
        self,
        level: Level,
        title: str,
        body: str,
        key: str | None,
        context: Mapping[str, Any],
        dry_run: bool | None,
        use_throttle: bool,
    ) -> AlertResult:
        now = self.clock()
        throttle_id = key or f"{level}:{title}"
        fields = {
            "alert_level": level,
            "title": title,
            "throttle_id": throttle_id,
            "alert_context": dict(context),
        }
        log_at = getattr(log, level)

        if level == "info":
            log_at("alert", status="logged", **fields)
            return AlertResult(status="logged", throttle_id=throttle_id)

        try:
            alerts = self.settings.require("alerts")
        except ConfigError as exc:
            log.error("alert_not_configured", error=str(exc), **fields)
            return AlertResult(status="failed", throttle_id=throttle_id, error=str(exc))
        recipient = mask_email(alerts.alert_email_to)
        fields["recipient"] = recipient

        # Check and claim the throttle window under the lock, so a second program alerting at
        # the same moment sees the claim and stays quiet. SMTP runs outside the lock.
        with self.throttle.locked():
            if use_throttle and self._throttled(throttle_id, now):
                log_at("alert", status="suppressed", **fields)
                return AlertResult(
                    status="suppressed", throttle_id=throttle_id, recipient=recipient
                )

            message = self.build_message(level, title, body, context, now)
            if self.settings.alert_dry_run if dry_run is None else dry_run:
                log_at("alert", status="dry_run", **fields)
                return AlertResult("dry_run", throttle_id, recipient, message=message)

            previous = self.throttle.load().get(throttle_id)
            if use_throttle:
                self.throttle.record(throttle_id, now)

        error = self._deliver_with_retry(message)
        with self.throttle.locked():
            if error:
                if use_throttle:  # give the window back: the next attempt may send
                    self.throttle.restore(throttle_id, now, previous)
            elif not use_throttle:
                self.throttle.record(throttle_id, now)
        if error:
            log.error("alert", status="failed", error=error, **fields)
            return AlertResult("failed", throttle_id, recipient, error=error, message=message)

        log_at("alert", status="sent", **fields)
        return AlertResult("sent", throttle_id, recipient, message=message)

    def _throttled(self, throttle_id: str, now: datetime) -> bool:
        hours = self.settings.alert_throttle_hours
        last = self.throttle.last_sent(throttle_id)
        return hours > 0 and last is not None and now - last < timedelta(hours=hours)

    def build_message(
        self,
        level: Level,
        title: str,
        body: str,
        context: Mapping[str, Any],
        now: datetime,
    ) -> EmailMessage:
        """The full email. Subject, body and context are masked like log lines."""
        alerts = self.settings.require("alerts")
        safe_title = mask_text(" ".join(title.split()))  # no newlines in a header
        local = now.astimezone(ZoneInfo(self.settings.display_timezone))
        lines = [
            safe_title,
            "",
            f"Level:   {level.upper()}",
            f"Time:    {now.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC",
            f"         {local:%Y-%m-%d %H:%M:%S} {self.settings.display_timezone}",
            f"Profile: {self.settings.app_profile}",
            "",
            mask_text(body),
        ]
        masked_context = mask_value(dict(context))
        if masked_context:
            lines += ["", "Context:"]
            lines += [f"  {k}: {v}" for k, v in masked_context.items()]
        lines += ["", "--", FOOTER]

        msg = EmailMessage()
        msg["Subject"] = f"[market-intel][{level.upper()}] {safe_title}"
        msg["From"] = alerts.alert_email_from
        msg["To"] = alerts.alert_email_to
        msg.set_content("\n".join(lines))
        return msg

    def _deliver_with_retry(self, message: EmailMessage) -> str | None:
        """Send, retrying once. Returns None on success or a masked error description."""
        error = None
        for attempt in (1, 2):
            try:
                self._deliver(message)
                return None
            except Exception as exc:
                error = f"{type(exc).__name__}: {mask_text(str(exc))}"
                log.warning("alert_send_attempt_failed", attempt=attempt, error=error)
                if attempt == 1:
                    self.sleep(RETRY_DELAY_S)
        return error

    def _deliver(self, message: EmailMessage) -> None:
        alerts = self.settings.require("alerts")
        password = alerts.alert_email_app_password.get_secret_value().replace(" ", "")
        host, port = self.settings.alert_smtp_host, self.settings.alert_smtp_port
        context = ssl.create_default_context()
        if port == SSL_PORT:
            # Port 465: TLS from the first byte.
            with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context) as smtp:
                smtp.login(alerts.alert_email_from, password)
                smtp.send_message(message)
        else:
            # Port 587: plain connect, then upgrade with STARTTLS before logging in.
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S) as smtp:
                smtp.starttls(context=context)
                smtp.login(alerts.alert_email_from, password)
                smtp.send_message(message)


class BackgroundAlerts:
    """Sends alerts on a background thread, so a job never waits for SMTP.

    ``send`` queues and returns at once. ``flush`` waits (bounded) until the queue is empty;
    it is registered with ``atexit`` so queued alerts still go out when the program ends.
    """

    def __init__(
        self,
        sender: AlertSender,
        *,
        flush_timeout_s: float = FLUSH_TIMEOUT_S,
        register_atexit: bool = True,
    ) -> None:
        self.sender = sender
        self.flush_timeout_s = flush_timeout_s
        self._queue: queue.Queue[tuple[tuple[Any, ...], dict[str, Any]]] = queue.Queue()
        self._pending = 0
        self._done = threading.Condition()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        if register_atexit:
            atexit.register(self.flush)

    def send(self, level: Level, title: str, body: str, **kwargs: Any) -> None:
        """Queue one alert (same arguments as AlertSender.send) and return at once."""
        with self._done:
            self._pending += 1
        self._queue.put(((level, title, body), kwargs))
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                # daemon: a stuck SMTP server can't keep the program alive past flush()
                self._thread = threading.Thread(target=self._run, name="alert-sender", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            args, kwargs = self._queue.get()
            try:
                self.sender.send(*args, **kwargs)  # never raises
            except BaseException as exc:  # last resort: keep the thread alive
                log.error("alert_background_error", error_type=type(exc).__name__)
            finally:
                with self._done:
                    self._pending -= 1
                    self._done.notify_all()

    @property
    def pending(self) -> int:
        with self._done:
            return self._pending

    def flush(self, timeout_s: float | None = None) -> bool:
        """Wait until every queued alert is handled, at most ``timeout_s`` (default
        FLUSH_TIMEOUT_S). Returns False (and logs it) if alerts were still queued."""
        limit = self.flush_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + limit
        with self._done:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("alerts_not_flushed", pending=self._pending, waited_s=limit)
                    return False
                self._done.wait(remaining)
        return True


_background: BackgroundAlerts | None = None
_background_lock = threading.Lock()


def background_alerts() -> BackgroundAlerts:
    """The process-wide background sender (created on first use, with the app's settings)."""
    global _background
    with _background_lock:
        if _background is None:
            _background = BackgroundAlerts(AlertSender(get_settings()))
        return _background


def queue_alert(
    level: Level,
    title: str,
    body: str,
    *,
    key: str | None = None,
    context: Mapping[str, Any] | None = None,
    dry_run: bool | None = None,
) -> None:
    """Like send_alert, but returns at once: the email goes out on a background thread."""
    background_alerts().send(level, title, body, key=key, context=context, dry_run=dry_run)


def send_alert(
    level: Level,
    title: str,
    body: str,
    *,
    key: str | None = None,
    context: Mapping[str, Any] | None = None,
    dry_run: bool | None = None,
) -> AlertResult:
    """Send an alert with the app's settings. See the module docstring."""
    return AlertSender(get_settings()).send(
        level, title, body, key=key, context=context, dry_run=dry_run
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m market_intel.alerts --test [--dry-run]``: one manual warning alert."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.alerts")
    parser.add_argument("--test", action="store_true", help="send one warning-level test alert")
    parser.add_argument("--dry-run", action="store_true", help="build the email, don't send")
    args = parser.parse_args(argv)
    if not args.test:
        parser.print_help()
        return 2

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, "alerts")

    result = AlertSender(settings).send(
        "warning",
        "Test alert",
        "Manual test of the market-intel alert email. No action needed.",
        context={"trigger": "python -m market_intel.alerts --test"},
        dry_run=True if args.dry_run else None,
        use_throttle=False,  # a manual check always goes out
    )
    print(f"status: {result.status}")
    print(f"recipient: {result.recipient or '(not configured)'}")
    if result.error:
        print(f"error: {result.error}")
    return 0 if result.status in ("sent", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
