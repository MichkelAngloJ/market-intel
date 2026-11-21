"""Per-company news from Google News RSS search feeds (task 1.2b; decision 2026-11-22).

One feed per company: ``<GOOGLE_NEWS_RSS_URL>?q="<clean name>" when:2d&hl=en-IN&gl=IN&ceid=IN:en``
(checked 2026-11-22: an RSS 2.0 feed whose items have title, link, guid, pubDate, an HTML
description and ``<source url="...">publisher</source>``; no ETag or Last-Modified, so there
are no conditional requests).

Polite and honest:

- The project's user agent (never a browser's); one request per ``GOOGLE_NEWS_MIN_SECONDS``
  across the process (a :class:`~market_intel.rate_limit.RateLimiter`); redirects are NOT
  followed; item links (``news.google.com/rss/articles/...``) are stored, never opened.
- **Block detection:** HTTP 403, 429 or 503; a body that isn't a feed (an HTML page); or a
  redirect to a consent, "sorry" or CAPTCHA page. Any of these raises :class:`GoogleBlocked`;
  the caller stops the round and pauses all Google requests until the next IST day. Nothing
  is retried and the identity never changes.

``match_confidence`` is ``low`` when the clean name is a single word, or when it doesn't
appear in the title or summary (case-insensitive); otherwise ``high``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import feedparser
import httpx

from market_intel.rate_limit import RateLimiter, Window
from market_intel.sources.records import NewsRecord
from market_intel.sources.rss import (
    IST,
    SUMMARY_MAX,
    TIMEOUT,
    TITLE_MAX,
    USER_AGENT,
    _feed_time,
    strip_html,
    timing,
)

SOURCE = "google_news"
FEED_NAME = "google_news"
BLOCK_STATUSES = frozenset({403, 429, 503})
_BLOCK_WORDS = re.compile(r"consent|sorry|captcha|recaptcha", re.IGNORECASE)
_DROP_WORDS = {"limited", "ltd"}


class GoogleBlocked(Exception):
    """Google refused automated access. Stop the round; pause until the next IST day."""


class CompanyFetchError(Exception):
    """This company's request failed for another reason (network, 404, ...)."""


# --- names, queries, confidence ----------------------------------------------------------


def clean_name(name: str) -> str:
    """The company name without Limited/Ltd/Ltd. and a leading "The", spacing normalised."""
    words = name.split()
    if words and words[0].casefold() == "the":
        words = words[1:]
    kept = [w for w in words if w.casefold().strip(".,") not in _DROP_WORDS]
    return " ".join(kept)


def query_url(base: str, clean: str) -> str:
    """The search feed for one company: the exact name, the last 2 days, India English."""
    params = {"q": f'"{clean}" when:2d', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    return f"{base}?{urlencode(params, quote_via=quote)}"


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def match_confidence(clean: str, title: str, summary: str | None) -> str:
    name = _norm(clean)
    if len(name.split()) <= 1:
        return "low"
    return "high" if name in _norm(title) or name in _norm(summary) else "low"


def next_ist_midnight(now: datetime) -> datetime:
    """The start of the next IST day, in UTC: when a block pause ends."""
    tomorrow = now.astimezone(IST).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), IST).astimezone(UTC)


_limiters: dict[float, RateLimiter] = {}


def process_limiter(min_seconds: float) -> RateLimiter:
    """The one process-wide limiter: at most one Google request per ``min_seconds``."""
    if min_seconds not in _limiters:
        _limiters[min_seconds] = RateLimiter(SOURCE, [Window(1, min_seconds)], headroom=1.0)
    return _limiters[min_seconds]


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False)


# --- one company -------------------------------------------------------------------------


@dataclass
class CompanyFeed:
    records: list[NewsRecord] = field(default_factory=list)
    malformed: int = 0
    without_time: int = 0


def _looks_like_html(content_type: str, body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return "html" in content_type.lower() or head.startswith((b"<!doctype html", b"<html"))


def fetch(client: httpx.Client, url: str, *, limiter: RateLimiter, max_bytes: int) -> bytes:
    """One rate-limited GET of a company's search feed. Raises GoogleBlocked on any block
    signal, CompanyFetchError on other failures."""
    limiter.acquire()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
    try:
        with client.stream("GET", url, headers=headers, timeout=TIMEOUT) as response:
            status = response.status_code
            if status in BLOCK_STATUSES:
                raise GoogleBlocked(f"HTTP {status}")
            if 300 <= status < 400:
                location = response.headers.get("Location", "")
                if _BLOCK_WORDS.search(location):
                    raise GoogleBlocked(f"HTTP {status} redirect to a consent or CAPTCHA page")
                raise CompanyFetchError(f"HTTP {status} redirect (not followed)")
            if status >= 400:
                raise CompanyFetchError(f"HTTP {status}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise CompanyFetchError(f"feed larger than {max_bytes} bytes")
            content_type = response.headers.get("Content-Type", "")
    except httpx.HTTPError as exc:
        raise CompanyFetchError(f"{type(exc).__name__}: {exc}") from None
    if _looks_like_html(content_type, bytes(body)):
        raise GoogleBlocked("an HTML page instead of a feed")
    return bytes(body)


def parse(
    content: bytes, *, symbol: str, clean: str, feed_url: str, fetched_at: datetime
) -> CompanyFeed:
    """Parse one company's feed. A body that isn't a recognisable feed is a block signal."""
    parsed = feedparser.parse(
        content, resolve_relative_uris=False, sanitize_html=False, response_headers={}
    )
    if not parsed.get("version") and not parsed.entries:
        raise GoogleBlocked("the response is not a feed")
    result = CompanyFeed()
    seen: set[str] = set()
    for entry in parsed.entries:
        title = strip_html(entry.get("title"), TITLE_MAX)
        link = entry.get("link")
        link = link.strip() if isinstance(link, str) else ""
        if not title or not link.startswith(("https://", "http://")):
            result.malformed += 1
            continue
        if link in seen:
            continue
        seen.add(link)
        summary = strip_html(entry.get("summary") or entry.get("description"), SUMMARY_MAX)
        published_at, source = timing(_feed_time(entry), fetched_at)
        if source == "fetched":
            result.without_time += 1
        publisher = _publisher(entry)
        result.records.append(
            NewsRecord(
                source=SOURCE,
                feed_url=feed_url,
                url=link,
                title=title,
                summary=summary,
                published_at=published_at,
                fetched_at=fetched_at,
                published_at_source=source,
                kind="company_news",
                publisher=publisher,
                feed_name=FEED_NAME,
                symbols=[symbol],
                match_confidence=match_confidence(clean, title, summary),
            )
        )
    return result


def _publisher(entry: Any) -> str | None:
    src = dict.get(entry, "source")
    if isinstance(src, dict):
        title = src.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:200]
    return None
