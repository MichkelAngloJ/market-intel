"""RSS feeds: the feed list, polite fetching and parsing into ``NewsRecord`` objects.

Legitimate channels only (decision 2026-11-22):

- An honest user agent, ``market-intel/<version> (personal research; RSS reader)``; never a
  browser's. A refusal (403, 429, 5xx) is recorded, ``Retry-After`` is respected, and the
  request is never repeated with another identity.
- Conditional requests (ETag / Last-Modified), a timeout, a size cap (``NEWS_FETCH_MAX_BYTES``)
  and at most one request per ``min_interval_minutes`` per feed.
- Item links (article pages, PDFs) are **never** followed: only the feed itself is fetched.

Parsing uses feedparser on the fetched BYTES (never a URL, so feedparser can't fetch anything
itself). Titles and summaries are stripped of HTML and capped. Time rules (leakage safety):
``published_at`` is the feed's date in UTC; missing -> the fetch time (``fetched``); more than
5 minutes in the future -> the fetch time (``clamped``); otherwise ``feed``. Exchange feeds
give IST times without a zone ("21-Nov-2026 18:30:00"); those are read as IST.
"""

from __future__ import annotations

import calendar
import email.utils
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import feedparser
import httpx

from market_intel import __version__
from market_intel.mongo.models import ExchangeMeta, exchange_identity, item_key
from market_intel.sources.records import NewsRecord, check_tag

USER_AGENT = f"market-intel/{__version__} (personal research; RSS reader)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DEFAULT_MIN_INTERVAL_MINUTES = 10
TITLE_MAX = 500
SUMMARY_MAX = 2000
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_RETRY_AFTER = timedelta(hours=24)
IST = ZoneInfo("Asia/Kolkata")
KINDS = ("news", "exchange")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Exchange feeds: "21-Nov-2026 18:30:00" or "21-Nov-2026 18:30" (IST, no zone)
_EXCHANGE_DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M")
_BSE_CODE = re.compile(r"\s*\((\d{3,8})\)\s*$")
_SUBJECT = re.compile(r"\|\s*SUBJECT\s*:\s*(.+?)\s*(?:\||$)", re.IGNORECASE | re.DOTALL)


class FeedConfigError(Exception):
    """The feed list is missing or invalid."""


class FeedFetchError(Exception):
    """The request failed: network error, HTTP error, too big, or not https."""

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- feed list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: Literal["news", "exchange"]
    publisher: str
    min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES
    enabled: bool = True

    @property
    def source(self) -> str:
        """The source tag stored on every item and used by source health."""
        return f"rss:{self.name}"


def load_feeds(path: Path) -> list[Feed]:
    """Read and check ``config/news_feeds.toml`` (tomllib)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FeedConfigError(f"feed list not found: {path.name}") from None
    except tomllib.TOMLDecodeError as exc:
        raise FeedConfigError(f"{path.name} is not valid TOML: {exc}") from None
    raw = data.get("feed")
    if not isinstance(raw, list) or not raw:
        raise FeedConfigError(f"{path.name} has no [[feed]] entries")
    feeds, names = [], set()
    for i, entry in enumerate(raw, 1):
        where = f"{path.name} feed {i}"
        if not isinstance(entry, dict):
            raise FeedConfigError(f"{where}: not a table")
        unknown = set(entry) - {
            "name",
            "url",
            "kind",
            "publisher",
            "min_interval_minutes",
            "enabled",
        }
        if unknown:
            raise FeedConfigError(f"{where}: unknown keys {sorted(unknown)}")
        name, url, kind = entry.get("name"), entry.get("url"), entry.get("kind")
        publisher = entry.get("publisher")
        interval = entry.get("min_interval_minutes", DEFAULT_MIN_INTERVAL_MINUTES)
        enabled = entry.get("enabled", True)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise FeedConfigError(f"{where}: name must be lower-case letters, digits and _")
        if name in names:
            raise FeedConfigError(f"{where}: duplicate name {name!r}")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise FeedConfigError(f"{where} ({name}): url must start with https://")
        if kind not in KINDS:
            raise FeedConfigError(f"{where} ({name}): kind must be news or exchange")
        if not isinstance(publisher, str) or not publisher.strip():
            raise FeedConfigError(f"{where} ({name}): publisher is required")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
            raise FeedConfigError(f"{where} ({name}): min_interval_minutes must be >= 1")
        if not isinstance(enabled, bool):
            raise FeedConfigError(f"{where} ({name}): enabled must be true or false")
        check_tag(f"rss:{name}")
        names.add(name)
        feeds.append(Feed(name, url, kind, publisher.strip(), interval, enabled))
    return feeds


# --- fetching ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """``content`` is None when the server answered 304 Not Modified."""

    content: bytes | None
    etag: str | None = None
    last_modified: str | None = None


def retry_after(value: str | None, now: datetime) -> timedelta | None:
    """Seconds or an HTTP date, capped at 24 hours."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        delay = timedelta(seconds=int(value))
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delay = when - now
    return max(timedelta(0), min(delay, MAX_RETRY_AFTER))


def fetch(
    client: httpx.Client,
    feed: Feed,
    *,
    max_bytes: int,
    etag: str | None = None,
    last_modified: str | None = None,
    now: datetime,
) -> FetchResult:
    """One polite GET of the feed itself. Raises FeedFetchError on any failure."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with client.stream("GET", feed.url, headers=headers, timeout=TIMEOUT) as response:
            if response.status_code == 304:
                return FetchResult(None, etag, last_modified)
            if response.status_code >= 400:
                wait = retry_after(response.headers.get("Retry-After"), now)
                raise FeedFetchError(f"HTTP {response.status_code}", retry_after=wait)
            if not str(response.url).startswith("https://"):
                raise FeedFetchError("the feed was served without https")
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise FeedFetchError(f"feed too large: {int(length)} bytes (limit {max_bytes})")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FeedFetchError(f"feed larger than {max_bytes} bytes")
            return FetchResult(
                bytes(body), response.headers.get("ETag"), response.headers.get("Last-Modified")
            )
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"{type(exc).__name__}: {exc}") from None


def make_client() -> httpx.Client:
    # Redirects are followed (a feed may move to a new address) but never to plain http.
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, max_redirects=3)


# --- parsing -----------------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: Any, limit: int) -> str | None:
    """Text without tags or entities, whitespace collapsed, at most ``limit`` characters."""
    if not isinstance(value, str):
        return None
    parser = _Text()
    parser.feed(value)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _feed_time(entry: Any) -> datetime | None:
    # dict.get reads the stored keys only (feedparser's own get maps updated <-> published)
    for key in ("published_parsed", "updated_parsed"):
        parsed = dict.get(entry, key)
        if parsed:  # feedparser gives a UTC struct_time
            return datetime.fromtimestamp(calendar.timegm(parsed), UTC)
    for key in ("published", "updated"):
        raw = dict.get(entry, key)
        if isinstance(raw, str):
            for fmt in _EXCHANGE_DATE_FORMATS:
                try:
                    return datetime.strptime(raw.strip(), fmt).replace(tzinfo=IST).astimezone(UTC)
                except ValueError:
                    continue
    return None


def timing(feed_time: datetime | None, fetched_at: datetime) -> tuple[datetime, str]:
    """``(published_at, published_at_source)`` by the time rules (see the module docstring)."""
    if feed_time is None:
        return fetched_at, "fetched"
    if feed_time > fetched_at + FUTURE_TOLERANCE:
        return fetched_at, "clamped"
    return feed_time, "feed"


def exchange_meta(
    feed: Feed, entry: Any, title: str, summary: str | None, link: str
) -> ExchangeMeta:
    """Company, code and subject as the exchange feed gives them (raw text, no guessing)."""
    company, code, subject = title, None, None
    if feed.publisher.upper() == "BSE":
        found = _BSE_CODE.search(title)
        if found:
            company = title[: found.start()].strip()
            code = found.group(1)
        scrip = entry.get("scripcode")
        if isinstance(scrip, str) and scrip.strip().isdigit():
            code = scrip.strip()
        subject = summary
    elif summary:
        found = _SUBJECT.search(summary)
        subject = found.group(1).strip() if found else None
    return ExchangeMeta(
        exchange=feed.publisher.upper(),
        company_raw=company or None,
        symbol_raw=code,
        subject=subject[:SUMMARY_MAX] if subject else None,
        attachment_url=link,
    )


@dataclass
class ParsedFeed:
    records: list[NewsRecord] = field(default_factory=list)
    malformed: int = 0  # entries without a link or a title
    without_time: int = 0  # usable items whose published_at came from the fetch time
    recognised: bool = True  # False: the body wasn't a recognisable RSS/Atom feed

    @property
    def entries(self) -> int:
        return len(self.records) + self.malformed


def item_key_for(
    feed: Feed, entry: Any, link: str, title: str, published_at: datetime, source: str
) -> str:
    """The item key (``url_hash``). Exchange items: link + the feed's guid, or + the title and
    time when there is no guid; repeated links are then separate events. News: the link."""
    if feed.kind != "exchange":
        return item_key(link)
    guid = dict.get(entry, "id")
    if isinstance(guid, str) and guid.strip():
        return item_key(link, guid.strip())
    return item_key(link, exchange_identity(title, published_at, source))


def parse(feed: Feed, content: bytes, fetched_at: datetime) -> ParsedFeed:
    """Parse one fetched feed body into records (see the module docstring)."""
    parsed = feedparser.parse(
        content, resolve_relative_uris=False, sanitize_html=False, response_headers={}
    )
    result = ParsedFeed(recognised=bool(parsed.get("version")) or bool(parsed.entries))
    seen: set[str] = set()
    for entry in parsed.entries:
        title = strip_html(entry.get("title"), TITLE_MAX)
        link = entry.get("link")
        link = link.strip() if isinstance(link, str) else ""
        if not title or not link.startswith(("https://", "http://")):
            result.malformed += 1
            continue
        summary = strip_html(entry.get("summary") or entry.get("description"), SUMMARY_MAX)
        published_at, source = timing(_feed_time(entry), fetched_at)
        key = item_key_for(feed, entry, link, title, published_at, source)
        if key in seen:
            continue  # the same item twice in one feed: keep the first
        seen.add(key)
        if source == "fetched":
            result.without_time += 1
        result.records.append(
            NewsRecord(
                url_hash=key,
                source=feed.source,
                feed_url=feed.url,
                url=link,
                title=title,
                summary=summary,
                published_at=published_at,
                fetched_at=fetched_at,
                published_at_source=source,
                kind=feed.kind,
                publisher=feed.publisher,
                feed_name=feed.name,
                exchange_meta=exchange_meta(feed, entry, title, summary, link)
                if feed.kind == "exchange"
                else None,
            )
        )
    return result
