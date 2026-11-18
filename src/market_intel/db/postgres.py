"""Postgres via psycopg 3: connection and health check."""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from market_intel.config import DatabaseSettings
from market_intel.db.common import (
    REFUSAL,
    DbReport,
    default_clock,
    is_local,
    median_latency_ms,
    parse_url,
    query_params,
    redactor,
)
from market_intel.logging_setup import get_logger

APP_NAME = "market-intel"
RETRY_DELAY_S = 3
# Only these sslmodes guarantee encryption; disable/allow/prefer (or none: libpq's default is
# prefer) may fall back to plain text and send the password unencrypted.
ENCRYPTED_SSLMODES = {"require", "verify-ca", "verify-full"}

log = get_logger("market_intel.db.postgres")


def connect(db: DatabaseSettings, *, sleep: Callable[[float], None] | None = None):
    """Open a connection. TLS settings (e.g. sslmode=require) come from the URL unchanged.

    Retries once after 3 s: Neon's compute sleeps when idle and the first connect can time out.
    """
    sleep = sleep or time.sleep
    url = db.postgres_url.get_secret_value()
    redact = redactor(url)
    for attempt in (1, 2):
        try:
            # Keyword arguments are merged into the URL's settings; they don't replace them.
            return psycopg.connect(
                url,
                connect_timeout=db.db_connect_timeout_s,
                application_name=APP_NAME,
                autocommit=True,
            )
        except psycopg.OperationalError as exc:
            if attempt == 2:
                raise
            log.warning("postgres_connect_retry", attempt=attempt, error=redact(str(exc)))
            sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")


def url_tls(url: str) -> bool:
    """Does the connection string force TLS? Read from the URL only, before connecting."""
    return query_params(url).get("sslmode", "").lower() in ENCRYPTED_SSLMODES


def check(
    db: DatabaseSettings,
    *,
    connector: Callable[[DatabaseSettings], psycopg.Connection] = connect,
    clock: Callable[[], float] = default_clock,
) -> DbReport:
    """Reachability, version, latency, client-side TLS, storage and a write test."""
    url = db.postgres_url.get_secret_value()
    redact = redactor(url)
    report = DbReport(name="postgres", host=parse_url(url).hosts, limit_mb=db.pg_storage_limit_mb)
    if not url_tls(url) and not is_local(report.host):
        # Refuse before connecting, so the password is never sent in plain text.
        report.tls, report.refused = False, True
        report.error = f"{REFUSAL} (sslmode must be require, verify-ca or verify-full)"
        return report
    try:
        with connector(db) as conn:
            report.reachable = True
            report.version = conn.execute("SHOW server_version").fetchone()[0]
            report.latency_ms = median_latency_ms(lambda: conn.execute("SELECT 1"), clock)
            # Client-side check: is *this* connection encrypted? (Neon's proxy terminates
            # TLS, so the server-side pg_stat_ssl view reports false even when it is.)
            report.tls = bool(conn.pgconn.ssl_in_use)
            size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            report.used_mb = round(size / 1024 / 1024, 1)
            report.write_ok = _write_test(conn)
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {redact(str(exc)).strip()}"
    return report


def _write_test(conn: psycopg.Connection) -> bool:
    # A TEMP table lives only in this session and is dropped at commit: nothing is left behind.
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE _healthcheck (id int, note text) ON COMMIT DROP")
        conn.execute("INSERT INTO _healthcheck VALUES (1, 'ok')")
        row = conn.execute("SELECT note FROM _healthcheck WHERE id = 1").fetchone()
    return row is not None and row[0] == "ok"
