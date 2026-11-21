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
from market_intel.mongo.models import NewsItem
from market_intel.source_health import SourceHealth, load_state, state_path
from market_intel.sources import rss
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
    """Active NSE stocks by symbol and by normalised company name."""

    def __init__(self, rows: Iterable[tuple[str, str | None]]) -> None:
        self.symbols: set[str] = set()
        self.by_name: dict[str, set[str]] = {}
        for symbol, name in rows:
            self.symbols.add(symbol)
            if name:
                key = normalise_company(name)
                if key:
                    self.by_name.setdefault(key, set()).add(symbol)

    def match(self, exchange: str, company: str | None, code: str | None) -> tuple[list[str], str]:
        """``(symbols, outcome)`` with outcome 'matched', 'ambiguous' or 'unmatched'."""
        if exchange == "NSE" and code and code.upper() in self.symbols:
            return [code.upper()], "matched"
        found = self.by_name.get(normalise_company(company)) if company else None
        if not found:
            return [], "unmatched"
        if len(found) > 1:
            return [], "ambiguous"
        return sorted(found), "matched"


INSTRUMENTS_SQL = (
    "SELECT symbol, name FROM instruments"
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
        yield client[db.mongodb_db][COLLECTION]


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
        rows = client[db.mongodb_db][COLLECTION].aggregate(
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
            else:
                print("\n".join(status_lines(settings, tz=ZoneInfo(settings.display_timezone))))
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
