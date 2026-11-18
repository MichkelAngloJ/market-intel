"""Connection check for both databases: ``uv run python -m market_intel.db_check``.

For each database it prints reachable, host (only), version, latency, client-side TLS,
storage vs the free-tier limit and a write test. Exit code 0 if everything passed, 1 if not.

A connection to a non-local host without TLS is refused before connecting (exit 1).
A failure or refusal sends a CRITICAL alert, storage >= 80% a WARNING (throttled per
database). Without alert settings (e.g. in CI) it logs "alerts_not_configured" and carries on.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from market_intel.alerts import AlertSender
from market_intel.config import ConfigError, DatabaseSettings, Settings, get_settings
from market_intel.db import mongo, postgres
from market_intel.db.common import STORAGE_WARN_PCT, DbReport
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id

log = get_logger("market_intel.db_check")

Checker = Callable[[DatabaseSettings], DbReport]


def render(report: DbReport) -> str:
    """Human-readable block for one database. Contains the host only, never credentials."""

    def yes_no(value: bool | None) -> str:
        return "-" if value is None else ("yes" if value else "NO")

    reachable = "not tried (refused)" if report.refused else yes_no(report.reachable)
    lines = [
        report.name,
        f"  reachable : {reachable}",
        f"  host      : {report.host}",
    ]
    if report.reachable:
        storage = "-"
        if report.used_mb is not None:
            storage = f"{report.used_mb} / {report.limit_mb} MB ({report.used_pct:.1f}%)"
            if report.storage_warning:
                storage += f"  WARNING >= {STORAGE_WARN_PCT}%"
        lines += [
            f"  version   : {report.version}",
            f"  latency   : {report.latency_ms} ms (median of 3)",
            f"  tls       : {yes_no(report.tls)} (client-side)",
            f"  storage   : {storage}",
            f"  write test: {'ok' if report.write_ok else 'FAILED'}",
        ]
    for warning in report.warnings:
        lines.append(f"  warning   : {warning}")
    if report.error:
        lines.append(f"  error     : {report.error}")
    return "\n".join(lines)


def alert_on(report: DbReport, settings: Settings, sender: AlertSender) -> None:
    """CRITICAL alert when a check failed, WARNING when storage is >= 80%."""
    alerts = []
    if report.refused:
        alerts.append(
            (
                "critical",
                f"Database connection not encrypted: {report.name}",
                f"db_unencrypted:{report.name}",
                f"Refused to connect to {report.name} on host {report.host} without TLS.",
            )
        )
    elif not report.ok:
        alerts.append(
            (
                "critical",
                f"Database unreachable: {report.name}",
                f"db_unreachable:{report.name}",
                f"The {report.name} check failed on host {report.host}.\n{report.error or ''}",
            )
        )
    if report.storage_warning:
        alerts.append(
            (
                "warning",
                f"Database storage at {report.used_pct:.0f}%: {report.name}",
                f"db_storage:{report.name}",
                f"{report.name} uses {report.used_mb} of {report.limit_mb} MB.",
            )
        )
    for level, title, key, body in alerts:
        if settings.missing("alerts"):
            log.warning("alerts_not_configured", would_send=title, alert_level=level)
            continue
        result = sender.send(level, title, body, key=key, context={"host": report.host})
        print(f"  alert     : {level} -> {result.status}")


def run(
    settings: Settings,
    *,
    checkers: dict[str, Checker] | None = None,
    sender: AlertSender | None = None,
) -> int:
    """Check both databases, print the reports, send alerts. Returns the exit code."""
    db = settings.require("database")  # ConfigError names any missing variables
    checkers = checkers or {"postgres": postgres.check, "mongodb": mongo.check}
    sender = sender or AlertSender(settings)
    all_ok = True
    for name, checker in checkers.items():
        report = checker(db)
        (log.info if report.ok else log.error)(
            "db_check",
            database=name,
            host=report.host,
            reachable=report.reachable,
            version=report.version,
            latency_ms=report.latency_ms,
            tls=report.tls,
            used_mb=report.used_mb,
            limit_mb=report.limit_mb,
            write_ok=report.write_ok,
            error=report.error,
        )
        for warning in report.warnings:
            log.warning("db_check_warning", database=name, warning=warning)
        print(render(report))
        alert_on(report, settings, sender)
        print()
        all_ok = all_ok and report.ok
    print("result: OK" if all_ok else "result: FAILED")
    return 0 if all_ok else 1


def main() -> int:
    """Entry point for ``python -m market_intel.db_check``."""
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings)
    try:
        with bound_context(run_id=new_run_id(), component="db_check"):
            return run(settings)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
