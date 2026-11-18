"""Shared pieces for the database checks: the report type, URL parsing and redaction.

Only hosts may be shown. Users, passwords and full URLs never leave this module unredacted.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote

MASK = "***"
STORAGE_WARN_PCT = 80
PINGS = 3
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REFUSAL = "refusing unencrypted connection to non-local host"
# Profiles that run the databases in Docker, where one-word service names ("postgres") are
# local. On the restricted profile such a name could be any machine on the network.
SERVICE_NAME_PROFILES = {"full", "server"}


class InsecureConnectionError(Exception):
    """Raised before connecting when TLS is off for a non-local host. Names the host only."""


@dataclass
class DbReport:
    """Result of checking one database. Safe to print: contains no credentials."""

    name: str
    host: str
    reachable: bool = False
    version: str | None = None
    latency_ms: float | None = None
    tls: bool | None = None
    used_mb: float | None = None
    limit_mb: int | None = None
    write_ok: bool | None = None
    error: str | None = None
    refused: bool = False  # True when the check refused to connect without TLS
    warnings: list[str] = field(default_factory=list)  # shown, logged; don't fail the check

    @property
    def used_pct(self) -> float | None:
        if self.used_mb is None or not self.limit_mb:
            return None
        return 100 * self.used_mb / self.limit_mb

    @property
    def ok(self) -> bool:
        return self.reachable and bool(self.write_ok) and self.error is None

    @property
    def storage_warning(self) -> bool:
        return self.used_pct is not None and self.used_pct >= STORAGE_WARN_PCT


@dataclass(frozen=True)
class UrlParts:
    """Pieces of a connection URL. ``user``/``password`` exist only to be redacted."""

    hosts: str
    user: str
    password: str


def parse_url(url: str) -> UrlParts:
    """Split a connection URL into hosts, user and password. No network lookup (no SRV/DNS)."""
    rest = url.split("://", 1)[-1]
    netloc = rest.split("/", 1)[0].split("?", 1)[0]
    userinfo, _, hostlist = netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    hosts = ",".join(h.rsplit(":", 1)[0] if h.count(":") == 1 else h for h in hostlist.split(","))
    return UrlParts(hosts=hosts or "(unknown)", user=user, password=password)


def query_params(url: str) -> dict[str, str]:
    """Connection-string options with lower-cased names (both drivers treat them so)."""
    query = url.split("?", 1)[1] if "?" in url else ""
    return {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}


def _bare_host(host: str) -> str:
    """Host without port or IPv6 brackets: "[::1]:5432" -> "::1"."""
    if host.startswith("["):
        return host[1 : host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_local(hosts: str, *, service_names_local: bool = True) -> bool:
    """True if every host is this machine, or (when allowed) a Docker service name: one word,
    no dot. Connection code always decides ``service_names_local`` from the profile, through
    :func:`host_is_local`."""
    names = [_bare_host(h.strip()).lower() for h in hosts.split(",")]
    return all(
        n in LOCAL_HOSTS or (service_names_local and n and "." not in n and ":" not in n)
        for n in names
    )


def service_names_allowed(profile: str) -> bool:
    """One-word hosts count as local only on the Docker profiles (full, server)."""
    return profile in SERVICE_NAME_PROFILES


def host_is_local(hosts: str, profile: str) -> bool:
    """The local-host rule used by every connection function."""
    return is_local(hosts, service_names_local=service_names_allowed(profile))


def redactor(url: str) -> Callable[[str], str]:
    """Return a function that removes the URL, user and password from any text."""
    parts = parse_url(url)
    secrets = {url, parts.user, parts.password, unquote(parts.user), unquote(parts.password)}
    ordered = sorted((s for s in secrets if s), key=len, reverse=True)

    def redact(text: str) -> str:
        for secret in ordered:
            text = text.replace(secret, MASK)
        return text

    return redact


def median_latency_ms(ping: Callable[[], object], clock: Callable[[], float]) -> float:
    """Median round-trip time of ``PINGS`` calls, in milliseconds."""
    times = []
    for _ in range(PINGS):
        start = clock()
        ping()
        times.append((clock() - start) * 1000)
    return round(statistics.median(times), 1)


def default_clock() -> float:
    return time.perf_counter()
