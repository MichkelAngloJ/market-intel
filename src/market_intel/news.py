"""News and exchange feeds into MongoDB ``news_items`` (task 1.2).

    uv run python -m market_intel.news fetch [--feed NAME] [--force] [--dry-run]
    uv run python -m market_intel.news status

fetch: one ``ingestion_run(job="news_fetch", source="rss")``, logs to ``logs/news.jsonl``.

- Each enabled feed (or only ``--feed NAME``) is fetched politely (:mod:`market_intel.sources.rss`).
  A feed fetched less than ``min_interval_minutes`` ago, or still inside a ``Retry-After``
  wait, is skipped (``--force`` ignores the interval, never ``Retry-After``).
- New items are inserted with the app login; an item whose ``url_hash`` exists is a
  duplicate and the FIRST document stays unchanged.
- Exchange items get ``symbols`` only on a clear match with an ACTIVE NSE stock: the exact
  NSE symbol, or one exact normalised company name (case, punctuation, Ltd/Limited ignored).
  Ambiguous or no match: ``symbols = []`` (counted). News items: ``[]`` (Phase 2 extracts
  companies).
- Every attempt is reported to :mod:`market_intel.source_health` (alerts on broken sources).
- Run status: all attempted feeds OK (or nothing attempted) -> success; some failed and at
  least one OK -> partial (the reason names them); all attempted failed -> failed.
  ``rows_written`` = new documents.
- ``--dry-run`` fetches and parses but writes nothing (no documents, no feed state, no source
  health, no ingestion run).

Per-feed fetch state (ETag, Last-Modified, last fetch, Retry-After) lives in
``DATA_DIR/state/news_feeds.json``, written atomically under a file lock.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pymongo.errors import DuplicateKeyError

from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import mongo, postgres
from market_intel.file_lock import file_lock
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    mask_text,
    new_run_id,
)
from market_intel.mongo import access
from market_intel.mongo.models import NewsItem, exchange_identity, item_key
from market_intel.source_health import SourceHealth, load_state, state_path
from market_intel.sources import google_news, rss
from market_intel.sources.rss import Feed, FeedConfigError, FeedFetchError

COMPONENT = "news"
JOB = "news_fetch"
SOURCE = "rss"
COLLECTION = "news_items"
STATE_NAME = "news_feeds.json"

log = get_logger("market_intel.news")


class AllFeedsFailed(Exception):
    """Every attempted feed failed; the run is marked failed."""

    def __init__(self, message: str, summary: FetchSummary) -> None:
        super().__init__(message)
        self.summary = summary


# --- per-feed fetch state ----------------------------------------------------------------


def feed_state_path(data_dir: Path) -> Path:
    return data_dir / "state" / STATE_NAME


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


class FeedState:
    """``{feed name: {etag, last_modified, last_fetch_at, retry_until, last_items}}``."""

    def __init__(self, data_dir: Path) -> None:
        self.path = feed_state_path(data_dir)

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @contextmanager
    def update(self):
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            state = self.load()
            yield state
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)


def skip_reason(feed: Feed, entry: dict[str, Any], now: datetime, *, force: bool) -> str | None:
    """'retry_after' or 'interval' if this feed must not be fetched now, else None."""
    retry_until = _when(entry.get("retry_until"))
    if retry_until and now < retry_until:
        return "retry_after"  # never overridden, not even by --force
    last = _when(entry.get("last_fetch_at"))
    if not force and last and now - last < timedelta(minutes=feed.min_interval_minutes):
        return "interval"
    return None


# --- matching exchange items to instruments ----------------------------------------------

_SUFFIXES = {"LTD", "LIMITED"}


def normalise_company(name: str) -> str:
    """Upper case, '&' -> AND, punctuation removed, trailing Ltd/Limited dropped."""
    text = name.upper().replace("&", " AND ")
    words = re.sub(r"[^A-Z0-9]+", " ", text).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


class InstrumentIndex:
    """Active NSE stocks by symbol, by BSE scrip code and by normalised company name.

    ``rows`` are ``(symbol, name)`` or ``(symbol, name, bse_code)``.
    """

    def __init__(self, rows: Iterable[Sequence[Any]]) -> None:
        self.symbols: set[str] = set()
        self.by_bse: dict[str, str] = {}
        self.by_name: dict[str, set[str]] = {}
        for row in rows:
            symbol, name = row[0], row[1]
            bse_code = row[2] if len(row) > 2 else None
            self.symbols.add(symbol)
            if bse_code:
                self.by_bse[str(bse_code)] = symbol
            if name:
                key = normalise_company(name)
                if key:
                    self.by_name.setdefault(key, set()).add(symbol)

    def match(self, exchange: str, company: str | None, code: str | None) -> tuple[list[str], str]:
        """``(symbols, outcome)`` with outcome 'matched', 'ambiguous' or 'unmatched'."""
        if exchange == "NSE" and code and code.upper() in self.symbols:
            return [code.upper()], "matched"
        if exchange == "BSE" and code and code in self.by_bse:  # the exact scrip code first
            return [self.by_bse[code]], "matched"
        found = self.by_name.get(normalise_company(company)) if company else None
        if not found:
            return [], "unmatched"
        if len(found) > 1:
            return [], "ambiguous"
        return sorted(found), "matched"


INSTRUMENTS_SQL = (
    "SELECT symbol, name, bse_code FROM instruments"
    " WHERE exchange = 'NSE' AND segment = 'EQ' AND is_active ORDER BY symbol"
)


def load_index(settings: Settings, connect: Callable[..., Any] | None = None) -> InstrumentIndex:
    with (connect or postgres.connect)(settings.require("database")) as conn:
        return InstrumentIndex(conn.execute(INSTRUMENTS_SQL).fetchall())


# --- one fetch run -----------------------------------------------------------------------


@dataclass
class FeedSummary:
    name: str
    outcome: str = "ok"  # ok, not_modified, skipped:interval, skipped:retry_after, failed
    new: int = 0
    duplicate: int = 0
    items: int = 0
    malformed: int = 0
    without_time: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    error: str | None = None

    @property
    def attempted(self) -> bool:
        return not self.outcome.startswith("skipped")


@dataclass
class FetchSummary:
    feeds: list[FeedSummary] = field(default_factory=list)
    dry_run: bool = False
    status: str = "success"
    run_id: int | None = None

    @property
    def new(self) -> int:
        return sum(f.new for f in self.feeds)

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        failed = [f.name for f in self.feeds if f.outcome == "failed"]
        lines = [
            f"{prefix}news fetch: {self.status}; new {self.new},"
            f" duplicate {sum(f.duplicate for f in self.feeds)},"
            f" skipped {sum(not f.attempted or f.outcome == 'not_modified' for f in self.feeds)},"
            f" failed {len(failed)}"
        ]
        width = max((len(f.name) for f in self.feeds), default=4)
        for f in self.feeds:
            if f.outcome == "ok":
                extra = f"new {f.new}, duplicate {f.duplicate}, malformed {f.malformed}"
                if f.matched or f.ambiguous or f.unmatched:
                    extra += (
                        f"; symbols matched {f.matched}, ambiguous {f.ambiguous},"
                        f" unmatched {f.unmatched}"
                    )
            elif f.outcome == "failed":
                extra = f"failed: {f.error}"
            else:
                extra = f.outcome.replace("skipped:", "skipped (") + (
                    ")" if f.outcome.startswith("skipped:") else ""
                )
            lines.append(f"  {f.name:<{width}}  {extra}")
        return lines


def _insert_new(collection: Any, docs: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Insert documents whose url_hash isn't stored yet. Returns (new, duplicate)."""
    if not docs:
        return 0, 0
    hashes = [d["url_hash"] for d in docs]
    existing = {
        d["url_hash"] for d in collection.find({"url_hash": {"$in": hashes}}, {"url_hash": 1})
    }
    new = duplicate = 0
    for doc in docs:
        if doc["url_hash"] in existing:
            duplicate += 1
            continue
        try:
            collection.insert_one(doc)
            new += 1
        except DuplicateKeyError:  # inserted meanwhile: the first document wins
            duplicate += 1
    return new, duplicate


def fetch_feed(
    feed: Feed,
    *,
    client: httpx.Client,
    settings: Settings,
    entry: dict[str, Any],
    now: datetime,
    index: InstrumentIndex | None,
    collection: Any | None,
    health: SourceHealth | None,
) -> FeedSummary:
    """Fetch, parse, match and store one feed. Updates ``entry`` (its fetch state)."""
    summary = FeedSummary(feed.name)
    entry["last_fetch_at"] = _iso(now)
    try:
        got = rss.fetch(
            client,
            feed,
            max_bytes=settings.news_fetch_max_bytes,
            etag=entry.get("etag"),
            last_modified=entry.get("last_modified"),
            now=now,
        )
    except FeedFetchError as exc:
        summary.outcome, summary.error = "failed", mask_text(str(exc))
        if exc.retry_after:
            entry["retry_until"] = _iso(now + exc.retry_after)
        if health:
            health.record_failure(feed.source, summary.error)
        return summary
    entry.pop("retry_until", None)
    if got.content is None:
        summary.outcome = "not_modified"
        if health:
            health.record_not_modified(feed.source)
        return summary

    parsed = rss.parse(feed, got.content, now)
    summary.items, summary.malformed = len(parsed.records), parsed.malformed
    summary.without_time = parsed.without_time
    docs = []
    for record in parsed.records:
        symbols: list[str] = []
        meta = record.exchange_meta
        if meta is not None and index is not None:
            symbols, outcome = index.match(meta.exchange, meta.company_raw, meta.symbol_raw)
            setattr(summary, outcome, getattr(summary, outcome) + 1)
        item: NewsItem = record.model_copy(update={"symbols": symbols}).to_document()
        docs.append(item.to_document())
    if collection is not None:
        summary.new, summary.duplicate = _insert_new(collection, docs)
    else:
        summary.new = len(docs)  # dry run: what would be offered for insert
    entry["etag"], entry["last_modified"] = got.etag, got.last_modified
    entry["last_items"] = summary.items
    entry["last_content_at"] = _iso(now)
    if health:
        health.record_content(
            feed.source,
            items=parsed.entries - parsed.malformed,
            malformed=parsed.malformed,
            without_time=parsed.without_time,
            recognised=parsed.recognised,
        )
    return summary


def select_feeds(feeds: Sequence[Feed], name: str | None) -> list[Feed]:
    if name is None:
        return [f for f in feeds if f.enabled]
    chosen = [f for f in feeds if f.name == name]
    if not chosen:
        raise FeedConfigError(f"no feed named {name!r} in the feed list")
    return chosen


def run_fetch(
    settings: Settings,
    *,
    feed_name: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    collection: Any | None = None,
    index: InstrumentIndex | None = None,
    connect: Callable[..., Any] | None = None,
    health: SourceHealth | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FetchSummary:
    """The ``fetch`` command. ``client``, ``collection``, ``index``, ``connect`` and ``health``
    are for tests; by default the real network, the app logins and source health are used."""
    feeds = select_feeds(rss.load_feeds(settings.news_feeds_file), feed_name)
    summary = FetchSummary(dry_run=dry_run)
    state = FeedState(settings.data_dir)
    own_client = client is None
    client = client or rss.make_client()
    try:
        if dry_run:
            entries = state.load()
            for feed in feeds:
                entry = dict(entries.get(feed.name, {}))
                reason = skip_reason(feed, entry, now(), force=force)
                if reason:
                    summary.feeds.append(FeedSummary(feed.name, outcome=f"skipped:{reason}"))
                    continue
                summary.feeds.append(
                    fetch_feed(
                        feed,
                        client=client,
                        settings=settings,
                        entry=entry,
                        now=now(),
                        index=index,
                        collection=None,
                        health=None,
                    )
                )
            summary.status = _status(summary)
            return summary

        health = health or SourceHealth(settings.data_dir)
        if index is None:
            index = load_index(settings, connect)
        params = {"feed": feed_name, "force": force}
        with ingestion_run(settings, JOB, source=SOURCE, params=params, connect=connect) as run:
            summary.run_id = run.id
            with _collection(settings, collection) as coll:
                for feed in feeds:
                    with state.update() as entries:
                        entry = entries.setdefault(feed.name, {})
                        reason = skip_reason(feed, entry, now(), force=force)
                        if reason:
                            summary.feeds.append(
                                FeedSummary(feed.name, outcome=f"skipped:{reason}")
                            )
                            continue
                        result = fetch_feed(
                            feed,
                            client=client,
                            settings=settings,
                            entry=entry,
                            now=now(),
                            index=index,
                            collection=coll,
                            health=health,
                        )
                    summary.feeds.append(result)
                    run.add_rows(result.new)
            summary.status = _status(summary)
            failed = [f.name for f in summary.feeds if f.outcome == "failed"]
            if summary.status == "failed":
                raise AllFeedsFailed(
                    f"all {len(failed)} attempted feeds failed: {', '.join(failed)}", summary
                )
            if summary.status == "partial":
                attempted = sum(f.attempted for f in summary.feeds)
                run.mark_partial(f"{len(failed)} of {attempted} feeds failed: {', '.join(failed)}")
    finally:
        if own_client:
            client.close()
    log.info("news_fetched", status=summary.status, new=summary.new, feeds=len(summary.feeds))
    return summary


def _status(summary: FetchSummary) -> str:
    attempted = [f for f in summary.feeds if f.attempted]
    failed = [f for f in attempted if f.outcome == "failed"]
    if attempted and len(failed) == len(attempted):
        return "failed"
    return "partial" if failed else "success"


@contextmanager
def _collection(settings: Settings, given: Any | None):
    if given is not None:
        yield given
        return
    db = settings.require("database")
    with mongo.client(db) as client:
        yield access.collection(client[db.mongodb_db], COLLECTION)


# --- migrate-exchange-keys ---------------------------------------------------------------


@dataclass
class MigrateSummary:
    updated: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)  # ids of items left unchanged
    dry_run: bool = False

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        lines = [
            f"{prefix}exchange item keys: updated {self.updated}, unchanged {self.unchanged},"
            f" conflicts {len(self.conflicts)}"
        ]
        lines += [f"  conflict (left unchanged): item {item_id}" for item_id in self.conflicts]
        return lines


def migrate_exchange_keys(
    collection: Any, *, dry_run: bool = False, only: dict[str, Any] | None = None
) -> MigrateSummary:
    """Recompute ``url_hash`` of stored exchange items as their item key (link + title + time;
    the exchange feeds have no guid). Update only; idempotent. If another item already holds
    the new key, the item is a conflict: nothing changes for it and it is reported."""
    summary = MigrateSummary(dry_run=dry_run)
    fields = {"url": 1, "url_hash": 1, "title": 1, "published_at": 1, "published_at_source": 1}
    # ``only`` narrows the items (integration tests touch only their own fake items)
    docs = list(collection.find({"kind": "exchange", **(only or {})}, fields))
    taken = {d["url_hash"] for d in docs}
    for doc in docs:
        published = doc["published_at"]
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        identity = exchange_identity(
            doc["title"], published, doc.get("published_at_source") or "feed"
        )
        new_key = item_key(doc["url"], identity)
        if new_key == doc["url_hash"]:
            summary.unchanged += 1
            continue
        if new_key in taken or collection.find_one({"url_hash": new_key}, {"_id": 1}):
            summary.conflicts.append(str(doc["_id"]))
            continue
        if not dry_run:
            try:
                result = collection.update_one(
                    {"_id": doc["_id"], "url_hash": doc["url_hash"]},
                    {"$set": {"url_hash": new_key}},
                )
            except DuplicateKeyError:
                summary.conflicts.append(str(doc["_id"]))
                continue
            if result.modified_count != 1:
                summary.conflicts.append(str(doc["_id"]))
                continue
        taken.discard(doc["url_hash"])
        taken.add(new_key)
        summary.updated += 1
    log.info(
        "exchange_keys_migrated",
        dry_run=dry_run,
        updated=summary.updated,
        unchanged=summary.unchanged,
        conflicts=len(summary.conflicts),
    )
    return summary


def run_migrate(settings: Settings, *, dry_run: bool, collection: Any | None = None):
    with _collection(settings, collection) as coll:
        return migrate_exchange_keys(coll, dry_run=dry_run)


# --- company-fetch: per-company news from Google News RSS --------------------------------

COMPANY_JOB = "company_news"
GOOGLE_STATE_NAME = "google_news.json"
WATCHLIST_SQL = (
    "SELECT i.symbol, i.name FROM watchlist w JOIN instruments i ON i.id = w.instrument_id"
    " WHERE i.is_active ORDER BY i.symbol"
)
NAMES_SQL = (
    "SELECT symbol, name FROM instruments"
    " WHERE exchange = 'NSE' AND segment = 'EQ' AND is_active AND symbol = ANY(%s)"
)


class CompanyFetchFailed(Exception):
    """Nothing was fetched and companies failed or Google blocked us; the run is failed."""

    def __init__(self, message: str, summary: CompanySummary) -> None:
        super().__init__(message)
        self.summary = summary


@dataclass(frozen=True)
class Target:
    symbol: str
    name: str
    reason: str  # "watchlist", "filer" or "symbol"


def select_targets(
    watchlist: Sequence[tuple[str, str | None]],
    filer_symbols: Sequence[str],
    names: dict[str, str],
    *,
    cap: int,
) -> list[Target]:
    """Every watchlist stock, then up to ``cap`` recent filers (newest first, distinct, not on
    the watchlist, with a known active stock name)."""
    targets = [Target(sym, name, "watchlist") for sym, name in watchlist if name]
    on_list = {t.symbol for t in targets} | {sym for sym, _ in watchlist}
    filers: list[Target] = []
    for sym in filer_symbols:
        if len(filers) >= cap:
            break
        if sym in on_list or any(f.symbol == sym for f in filers) or not names.get(sym):
            continue
        filers.append(Target(sym, names[sym], "filer"))
    return targets + filers


def recent_filer_symbols(collection: Any, now: datetime, hours: int = 24) -> list[str]:
    """Matched symbols of exchange items available in the last ``hours``, newest first."""
    cursor = collection.find(
        {
            "kind": "exchange",
            "available_at": {"$gte": now - timedelta(hours=hours)},
            "symbols.0": {"$exists": True},
        },
        {"symbols": 1, "available_at": 1},
    ).sort("available_at", -1)
    ordered: list[str] = []
    for doc in cursor:
        for sym in doc.get("symbols") or []:
            if sym not in ordered:
                ordered.append(sym)
    return ordered


class GoogleState:
    """``DATA_DIR/state/google_news.json``: the block pause and each company's last fetch."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state" / GOOGLE_STATE_NAME

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @contextmanager
    def update(self):
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            state = self.load()
            state.setdefault("companies", {})
            yield state
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)


@dataclass
class CompanySummary:
    dry_run: bool = False
    status: str = "success"
    run_id: int | None = None
    watchlist: int = 0
    filers: int = 0
    requests: int = 0
    skipped: int = 0
    new: int = 0
    shared: int = 0
    duplicate: int = 0
    high: int = 0
    low: int = 0
    malformed: int = 0
    without_time: int = 0
    failed: int = 0
    blocked: bool = False
    paused_until: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def fetched(self) -> int:
        return self.requests - self.failed - (1 if self.blocked else 0)

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        lines = [
            f"{prefix}company news: {self.status}; targets {self.watchlist + self.filers}"
            f" (watchlist {self.watchlist}, filers {self.filers}); requests {self.requests};"
            f" skipped {self.skipped} (interval)",
            f"  new {self.new}, shared {self.shared}, duplicate {self.duplicate},"
            f" malformed {self.malformed}; confidence high {self.high}, low {self.low}",
            f"  failed {self.failed}; blocked {'yes' if self.blocked else 'no'}",
        ]
        if self.paused_until:
            lines.append(f"  Google requests paused until {self.paused_until} (UTC)")
        return lines


def store_company_items(collection: Any, docs: Sequence[dict[str, Any]], symbol: str):
    """Insert new items; an article already stored (any company) gains ``symbol`` with
    $addToSet and nothing else changes. Returns (new, shared, duplicate)."""
    new = shared = duplicate = 0
    for doc in docs:
        existing = collection.find_one({"url_hash": doc["url_hash"]}, {"symbols": 1})
        if existing is None:
            try:
                collection.insert_one(doc)
                new += 1
                continue
            except DuplicateKeyError:
                pass
        result = collection.update_one(
            {"url_hash": doc["url_hash"]}, {"$addToSet": {"symbols": symbol}}
        )
        if result.modified_count:
            shared += 1
        else:
            duplicate += 1
    return new, shared, duplicate


def load_targets(
    settings: Settings,
    collection: Any,
    now: datetime,
    *,
    symbol: str | None,
    connect: Callable[..., Any] | None = None,
) -> list[Target]:
    with (connect or postgres.connect)(settings.require("database")) as conn:
        if symbol:
            rows = conn.execute(NAMES_SQL, ([symbol.upper()],)).fetchall()
            if not rows:
                raise FeedConfigError(f"{symbol.upper()} is not an active NSE stock")
            return [Target(rows[0][0], rows[0][1], "symbol")]
        watchlist = conn.execute(WATCHLIST_SQL).fetchall()
        filers = recent_filer_symbols(collection, now) if collection is not None else []
        names = dict(conn.execute(NAMES_SQL, (filers,)).fetchall()) if filers else {}
    return select_targets(watchlist, filers, names, cap=settings.google_news_filers_cap)


def _company_round(
    settings: Settings,
    targets: Sequence[Target],
    summary: CompanySummary,
    *,
    client: httpx.Client,
    limiter: Any,
    collection: Any | None,
    state: dict[str, Any],
    force: bool,
    now: Callable[[], datetime],
) -> str | None:
    """Fetch each due company. Returns the block reason if Google blocked us."""
    interval = timedelta(minutes=settings.google_news_company_interval_minutes)
    companies = state.setdefault("companies", {})
    for target in targets:
        last = _when((companies.get(target.symbol) or {}).get("last_fetch_at"))
        if not force and last and now() - last < interval:
            summary.skipped += 1
            continue
        clean = google_news.clean_name(target.name)
        url = google_news.query_url(settings.google_news_rss_url, clean)
        summary.requests += 1
        companies[target.symbol] = {"last_fetch_at": _iso(now())}
        try:
            body = google_news.fetch(
                client, url, limiter=limiter, max_bytes=settings.news_fetch_max_bytes
            )
            feed = google_news.parse(
                body, symbol=target.symbol, clean=clean, feed_url=url, fetched_at=now()
            )
        except google_news.GoogleBlocked as exc:
            return str(exc)
        except google_news.CompanyFetchError as exc:
            summary.failed += 1
            summary.errors.append(mask_text(str(exc)))
            log.warning("company_fetch_failed", reason=target.reason, error=mask_text(str(exc)))
            continue
        summary.malformed += feed.malformed
        docs = [r.to_document().to_document() for r in feed.records]
        summary.high += sum(d["match_confidence"] == "high" for d in docs)
        summary.low += sum(d["match_confidence"] == "low" for d in docs)
        if collection is not None:
            new, shared, dup = store_company_items(collection, docs, target.symbol)
            summary.new += new
            summary.shared += shared
            summary.duplicate += dup
        else:
            summary.new += len(docs)
        summary.without_time += feed.without_time
    return None


def run_company_fetch(
    settings: Settings,
    *,
    symbol: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    collection: Any | None = None,
    targets: Sequence[Target] | None = None,
    connect: Callable[..., Any] | None = None,
    health: SourceHealth | None = None,
    limiter: Any | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CompanySummary:
    """The ``company-fetch`` command (see the docs). ``client``, ``collection``, ``targets``,
    ``connect``, ``health`` and ``limiter`` are for tests."""
    summary = CompanySummary(dry_run=dry_run)
    gstate = GoogleState(settings.data_dir)
    limiter = limiter or google_news.process_limiter(settings.google_news_min_seconds)
    own_client = client is None
    client = client or google_news.make_client()
    try:
        with _maybe_collection(settings, collection, dry_run) as coll:
            if targets is None:
                targets = load_targets(settings, coll, now(), symbol=symbol, connect=connect)
            summary.watchlist = sum(t.reason == "watchlist" for t in targets)
            summary.filers = len(targets) - summary.watchlist
            paused = _when(gstate.load().get("blocked_until"))
            if paused and now() < paused:
                summary.paused_until = _iso(paused)  # never overridden, not even by --force
                log.info("company_news_paused", until=summary.paused_until)
                return summary
            if dry_run:
                state = gstate.load()
                reason = _company_round(
                    settings, targets, summary, client=client, limiter=limiter,
                    collection=None, state=state, force=force, now=now,
                )  # fmt: skip
                if reason:
                    _record_block(gstate, summary, reason, now(), health=None)
                summary.status = _company_status(summary)
                return summary

            health = health or SourceHealth(settings.data_dir)
            params = {"symbol": symbol, "force": force, "targets": len(targets)}
            with ingestion_run(
                settings, COMPANY_JOB, source=google_news.SOURCE, params=params, connect=connect
            ) as run:
                summary.run_id = run.id
                with gstate.update() as state:
                    reason = _company_round(
                        settings, targets, summary, client=client, limiter=limiter,
                        collection=coll, state=state, force=force, now=now,
                    )  # fmt: skip
                run.add_rows(summary.new)
                if reason:
                    _record_block(gstate, summary, reason, now(), health=health)
                elif summary.requests:
                    if summary.fetched:
                        health.record_content(
                            google_news.SOURCE,
                            items=summary.new + summary.shared + summary.duplicate,
                            malformed=summary.malformed,
                            without_time=summary.without_time,
                        )
                    else:
                        health.record_failure(google_news.SOURCE, summary.errors[-1])
                summary.status = _company_status(summary)
                if summary.status == "failed":
                    raise CompanyFetchFailed(
                        "nothing fetched: " + ("Google blocked us" if reason else
                                               f"{summary.failed} companies failed"),
                        summary,
                    )  # fmt: skip
                if summary.status == "partial":
                    run.mark_partial(
                        ("Google blocked us; round stopped. " if reason else "")
                        + f"{summary.failed} of {summary.requests} company requests failed"
                    )
    finally:
        if own_client:
            client.close()
    log.info(
        "company_news_fetched",
        status=summary.status,
        targets=summary.watchlist + summary.filers,
        requests=summary.requests,
        new=summary.new,
        blocked=summary.blocked,
    )
    return summary


def _record_block(
    gstate: GoogleState,
    summary: CompanySummary,
    reason: str,
    now: datetime,
    *,
    health: SourceHealth | None,
) -> None:
    until = google_news.next_ist_midnight(now)
    summary.blocked = True
    summary.paused_until = _iso(until)
    with gstate.update() as state:
        state["blocked_until"] = _iso(until)
        state["block_reason"] = mask_text(reason)
    log.warning("google_news_blocked", reason=mask_text(reason), until=summary.paused_until)
    if health is not None:
        health.record_block(google_news.SOURCE, f"google_news blocked: {reason}")


def _company_status(summary: CompanySummary) -> str:
    if not summary.blocked and not summary.failed:
        return "success"
    return "partial" if summary.fetched > 0 else "failed"


@contextmanager
def _maybe_collection(settings: Settings, given: Any | None, dry_run: bool):
    if given is not None:
        yield given
        return
    with _collection(settings, None) as coll:
        yield coll


# --- rematch-exchange --------------------------------------------------------------------


@dataclass
class RematchSummary:
    changed: int = 0
    unchanged: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        return [
            f"{prefix}exchange items re-matched: changed {self.changed}, unchanged"
            f" {self.unchanged}; now matched {self.matched}, ambiguous {self.ambiguous},"
            f" unmatched {self.unmatched}"
        ]


def rematch_exchange(
    collection: Any,
    index: InstrumentIndex,
    *,
    dry_run: bool = False,
    only: dict[str, Any] | None = None,
) -> RematchSummary:
    """Set exchange items' ``symbols`` again by BSE code, then by name (the 1.2 rules); only
    ``symbols`` changes, and only where the new match differs."""
    summary = RematchSummary(dry_run=dry_run)
    docs = collection.find({"kind": "exchange", **(only or {})}, {"symbols": 1, "exchange_meta": 1})
    for doc in docs:
        meta = doc.get("exchange_meta") or {}
        symbols, outcome = index.match(
            meta.get("exchange") or "", meta.get("company_raw"), meta.get("symbol_raw")
        )
        setattr(summary, outcome, getattr(summary, outcome) + 1)
        if list(doc.get("symbols") or []) == symbols:
            summary.unchanged += 1
            continue
        summary.changed += 1
        if not dry_run:
            collection.update_one({"_id": doc["_id"]}, {"$set": {"symbols": symbols}})
    log.info("exchange_rematched", dry_run=dry_run, changed=summary.changed)
    return summary


# --- status ------------------------------------------------------------------------------


def status_lines(
    settings: Settings, *, counts: dict[str, int] | None = None, tz: Any = None
) -> list[str]:
    """Per feed: last fetch, last success, items in the last content fetch, consecutive
    failures, documents stored. ``counts`` (documents per feed) is for tests."""
    feeds = rss.load_feeds(settings.news_feeds_file)
    fetch_state = FeedState(settings.data_dir).load()
    health = load_state(state_path(settings.data_dir))
    if counts is None:
        counts = document_counts(settings)

    def show(value: Any) -> str:
        when = _when(value)
        return when.astimezone(tz).strftime("%Y-%m-%d %H:%M") if when else "-"

    header = (
        f"{'feed':<22} {'on':<3} {'last fetch':<16} {'last success':<16} {'items':>5}"
        f" {'fails':>5} {'docs':>6}  problems"
    )
    lines = [header]
    for feed in feeds:
        f_state = fetch_state.get(feed.name, {})
        h_state = health.get(feed.source, {})
        problems = ", ".join(sorted(h_state.get("problems") or {})) or "-"
        items = f_state.get("last_items")
        lines.append(
            f"{feed.name:<22} {'yes' if feed.enabled else 'no':<3}"
            f" {show(f_state.get('last_fetch_at')):<16}"
            f" {show(h_state.get('last_success_at')):<16}"
            f" {'-' if items is None else items:>5}"
            f" {h_state.get('consecutive_failures', 0):>5}"
            f" {counts.get(feed.name, 0):>6}  {problems}"
        )
    return lines


def document_counts(settings: Settings) -> dict[str, int]:
    db = settings.require("database")
    with mongo.client(db) as client:
        rows = access.collection(client[db.mongodb_db], COLLECTION).aggregate(
            [{"$group": {"_id": "$feed_name", "n": {"$sum": 1}}}]
        )
        return {row["_id"]: row["n"] for row in rows if row["_id"]}


# --- entry point -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m market_intel.news")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_p = sub.add_parser("fetch", help="fetch the RSS feeds into news_items")
    fetch_p.add_argument("--feed", help="only this feed (by name)")
    fetch_p.add_argument("--force", action="store_true", help="ignore the minimum interval")
    fetch_p.add_argument("--dry-run", action="store_true", help="fetch and parse, save nothing")
    sub.add_parser("status", help="per-feed fetch and health status")
    migrate_p = sub.add_parser(
        "migrate-exchange-keys", help="recompute the item keys of stored exchange items"
    )
    migrate_p.add_argument("--dry-run", action="store_true", help="count only, save nothing")
    rematch_p = sub.add_parser(
        "rematch-exchange", help="match stored exchange items again (BSE code, then name)"
    )
    rematch_p.add_argument("--dry-run", action="store_true", help="count only, save nothing")
    company_p = sub.add_parser(
        "company-fetch", help="per-company news from Google News RSS (watchlist + recent filers)"
    )
    company_p.add_argument("--symbol", help="only this active NSE stock")
    company_p.add_argument(
        "--force", action="store_true", help="ignore the per-company interval (never a block pause)"
    )
    company_p.add_argument("--dry-run", action="store_true", help="fetch and parse, save nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.news``."""
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    from zoneinfo import ZoneInfo

    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            if args.command == "fetch":
                summary = run_fetch(
                    settings, feed_name=args.feed, force=args.force, dry_run=args.dry_run
                )
                print("\n".join(summary.lines()))
            elif args.command == "company-fetch":
                company = run_company_fetch(
                    settings, symbol=args.symbol, force=args.force, dry_run=args.dry_run
                )
                print("\n".join(company.lines()))
            elif args.command == "rematch-exchange":
                with _collection(settings, None) as coll:
                    result = rematch_exchange(coll, load_index(settings), dry_run=args.dry_run)
                print("\n".join(result.lines()))
            elif args.command == "migrate-exchange-keys":
                print("\n".join(run_migrate(settings, dry_run=args.dry_run).lines()))
            else:
                print("\n".join(status_lines(settings, tz=ZoneInfo(settings.display_timezone))))
        except CompanyFetchFailed as exc:
            print("\n".join(exc.summary.lines()))
            print(f"Failed: {exc}", file=sys.stderr)
            return 1
        except AllFeedsFailed as exc:
            print("\n".join(exc.summary.lines()))
            print(f"Failed: {exc}", file=sys.stderr)
            return 1
        except (FeedConfigError, ConfigError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
