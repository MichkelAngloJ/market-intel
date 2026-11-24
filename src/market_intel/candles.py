"""Daily candles into Postgres ``candles_daily`` (sprint S1; decision 2026-11-24).

    uv run python -m market_intel.candles daily [--universe indices|nifty500|watchlist|all]
        [--from YYYY-MM-DD] [--symbol S] [--dry-run]

- **Sources:** Upstox V3 historical candles first; yfinance as a backup for STOCKS only, used
  when Upstox fails for an instrument and range or no valid token exists. Indices: Upstox only.
  Every row is raw (not adjusted) and tagged with its source.
- **Precedence:** for the same instrument and day, an ``upstox`` row may replace a
  ``yfinance`` row, never the reverse. Unchanged rows aren't rewritten (a rerun writes 0).
- **Incremental:** each instrument continues from its last stored day + 1; ``--from`` also
  loads earlier history (default start for an empty instrument: 2015-01-01).
- **Trading calendar:** the days that have a Nifty 50 index candle. Nifty 50 is always loaded
  first.
- **Gaps:** calendar days between an instrument's first candle and the last trading day that
  have no candle are ``data_gaps`` rows (interval ``1d``, one per run of missing days). The
  loader retries them with the other source; a gap becomes ``filled`` once all its days are
  present, and ``unfillable`` when every source available for the instrument has failed
  (stocks: Upstox and yfinance; indices: Upstox). Gaps never tried with Upstox stay ``open``.
- **Resumable:** ``DATA_DIR/state/candles_progress.json`` records the instruments finished for
  the current target day, so a rerun of an interrupted backfill continues where it stopped.
- **Suspected split/bonus:** a day-over-day close ratio within 3% of 1/2, 1/3, 1/4, 1/5, 1/10,
  2/3 or 4/5 while Nifty 50 moved less than 5% that day -> a ``price_events`` row. Prices are
  never changed; S2 excludes these days from labels.
- One ``ingestion_run(job="candles_daily", source="upstox")``; ``rows_written`` = rows inserted
  or changed; ``partial`` if some instruments failed. Source health: ``upstox_candles`` and
  ``yfinance_candles`` (one report per source per run: instruments OK vs failed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as time_mod
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from market_intel import upstox
from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import postgres
from market_intel.file_lock import file_lock
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    mask_text,
    new_run_id,
)
from market_intel.source_health import SourceHealth
from market_intel.sources import daily_candles as dc
from market_intel.sources.records import DailyCandle

COMPONENT = "candles"
JOB = "candles_daily"
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_FROM = date(2015, 1, 1)
CALENDAR_KEY = "NSE_INDEX|Nifty 50"
UNIVERSES = ("indices", "nifty500", "watchlist", "all")
# Indices Upstox's historical API doesn't serve (S1 review answer 1): not in "indices".
NOT_SERVED_INDEX_PREFIX = "NSE_INDEX|BHARATBOND"
# A day's candle is complete after the close; before this IST time "today" isn't loaded yet.
DAY_READY_IST = time(18, 0)
SPLIT_RATIOS = (
    Decimal(1) / 2, Decimal(1) / 3, Decimal(1) / 4, Decimal(1) / 5, Decimal(1) / 10,
    Decimal(2) / 3, Decimal(4) / 5,
)  # fmt: skip
SPLIT_TOLERANCE = Decimal("0.03")
MARKET_STILL = Decimal("0.05")
PROGRESS_NAME = "candles_progress.json"
UPSTOX_HEALTH, YAHOO_HEALTH = "upstox_candles", "yfinance_candles"
# Upstox's "invalid instrument key" codes: the instrument isn't served by the historical API
# (seen 2026-11-24 for the BHARATBOND indices). Reported as unsupported, not as a failure.
UNSUPPORTED_CODES = ("UDAPI1021", "UDAPI100011")
YAHOO_GIVE_UP = 5

log = get_logger("market_intel.candles")


@dataclass(frozen=True)
class Inst:
    id: int
    symbol: str
    segment: str  # EQ or INDEX
    instrument_key: str | None

    @property
    def is_index(self) -> bool:
        return self.segment == "INDEX"


# --- pure helpers ------------------------------------------------------------------------


def last_trading_day(now: datetime) -> date:
    """Today (IST) once the day's candle is complete, otherwise yesterday."""
    local = now.astimezone(IST)
    return local.date() if local.time() >= DAY_READY_IST else local.date() - timedelta(days=1)


def ranges(days: Iterable[date], calendar: Sequence[date]) -> list[tuple[date, date]]:
    """Group missing calendar days into runs of consecutive calendar days."""
    wanted = set(days)
    out: list[tuple[date, date]] = []
    run: list[date] = []
    for day in calendar:
        if day in wanted:
            run.append(day)
        elif run:
            out.append((run[0], run[-1]))
            run = []
    if run:
        out.append((run[0], run[-1]))
    return out


def missing_days(have: Sequence[date], calendar: Sequence[date], until: date) -> list[date]:
    """Calendar days from the first stored day to ``until`` without a candle."""
    if not have:
        return []
    present, first = set(have), min(have)
    return [d for d in calendar if first <= d <= until and d not in present]


def suspected_splits(
    closes: Sequence[tuple[date, Decimal]], market: dict[date, Decimal]
) -> list[tuple[date, Decimal]]:
    """(day, ratio) where close/previous close is within 3% of a common split/bonus ratio and
    Nifty 50 moved less than 5% over the same two days."""
    events = []
    for (d0, c0), (d1, c1) in zip(closes, closes[1:], strict=False):
        if not c0:
            continue
        ratio = Decimal(c1) / Decimal(c0)
        if not any(abs(ratio / r - 1) <= SPLIT_TOLERANCE for r in SPLIT_RATIOS):
            continue
        m0, m1 = market.get(d0), market.get(d1)
        if m0 and m1 and abs(Decimal(m1) / Decimal(m0) - 1) < MARKET_STILL:
            events.append((d1, ratio.quantize(Decimal("0.000001"))))
    return events


# --- storage -----------------------------------------------------------------------------


class Store(Protocol):
    def instruments(self, universe: str | None, symbol: str | None) -> list[Inst]: ...
    def calendar_instrument(self) -> Inst | None: ...
    def stored_dates(self, inst_id: int) -> list[date]: ...
    def source_dates(self, inst_id: int, source: str) -> list[date]: ...
    def upsert(self, inst_id: int, candles: Sequence[DailyCandle]) -> int: ...
    def closes(self, inst_id: int, since: date) -> list[tuple[date, Decimal]]: ...
    def gaps(self, inst_id: int) -> list[tuple[int, date, date, str]]: ...
    def save_gaps(
        self, inst_id: int, rows: Sequence[tuple[date, date, str]], filled: Sequence[int],
        run_id: int | None,
    ) -> None: ...  # fmt: skip
    def add_price_events(self, inst_id: int, events: Sequence[tuple[date, Decimal]]) -> int: ...
    def gap_counts(self, ids: Sequence[int]) -> dict[str, int]: ...
    def price_event_count(self, ids: Sequence[int]) -> int: ...


def _ist_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), IST)


UNIVERSE_SQL = {
    "indices": (
        "SELECT id, symbol, segment, instrument_key FROM instruments"
        " WHERE exchange = 'NSE' AND segment = 'INDEX' AND is_active"
        " AND instrument_key IS NOT NULL"
        f" AND instrument_key NOT LIKE '{NOT_SERVED_INDEX_PREFIX}%%' ORDER BY id"
    ),
    "nifty500": (
        "SELECT i.id, i.symbol, i.segment, i.instrument_key FROM instruments i"
        " JOIN universe_members u ON u.instrument_id = i.id AND u.universe = 'nifty500'"
        " WHERE i.is_active ORDER BY i.id"
    ),
    "watchlist": (
        "SELECT i.id, i.symbol, i.segment, i.instrument_key FROM instruments i"
        " JOIN watchlist w ON w.instrument_id = i.id WHERE i.is_active ORDER BY i.id"
    ),
}


class PgStore:
    """The SQL behind :class:`Store` (app login). Each call commits on its own (autocommit),
    except where a transaction is opened by the loader."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def instruments(self, universe: str | None, symbol: str | None) -> list[Inst]:
        if symbol:
            rows = self.conn.execute(
                "SELECT id, symbol, segment, instrument_key FROM instruments"
                " WHERE exchange = 'NSE' AND symbol = %s AND is_active ORDER BY segment, id",
                (symbol,),
            ).fetchall()
            return [Inst(*r) for r in rows[:1]]
        names = ("indices", "nifty500", "watchlist") if universe == "all" else (universe,)
        seen: dict[int, Inst] = {}
        for name in names:
            for row in self.conn.execute(UNIVERSE_SQL[name]).fetchall():
                seen.setdefault(row[0], Inst(*row))
        return list(seen.values())

    def calendar_instrument(self) -> Inst | None:
        row = self.conn.execute(
            "SELECT id, symbol, segment, instrument_key FROM instruments WHERE instrument_key = %s",
            (CALENDAR_KEY,),
        ).fetchone()
        return Inst(*row) if row else None

    def stored_dates(self, inst_id: int) -> list[date]:
        rows = self.conn.execute(
            "SELECT trade_date FROM candles_daily WHERE instrument_id = %s ORDER BY 1", (inst_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def source_dates(self, inst_id: int, source: str) -> list[date]:
        rows = self.conn.execute(
            "SELECT trade_date FROM candles_daily WHERE instrument_id = %s AND source = %s"
            " ORDER BY 1",
            (inst_id, source),
        ).fetchall()
        return [r[0] for r in rows]

    def upsert(self, inst_id: int, candles: Sequence[DailyCandle]) -> int:
        if not candles:
            return 0
        sql = (
            "INSERT INTO candles_daily (instrument_id, trade_date, open, high, low, close,"
            " volume, oi, price_basis, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (instrument_id, trade_date) DO UPDATE SET open = EXCLUDED.open,"
            " high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,"
            " volume = EXCLUDED.volume, oi = EXCLUDED.oi, price_basis = EXCLUDED.price_basis,"
            " source = EXCLUDED.source, ingested_at = now()"
            # upstox may replace yfinance, never the reverse; unchanged rows aren't rewritten
            " WHERE NOT (candles_daily.source = 'upstox' AND EXCLUDED.source = 'yfinance')"
            " AND (candles_daily.open, candles_daily.high, candles_daily.low,"
            " candles_daily.close, candles_daily.volume, candles_daily.source)"
            " IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close,"
            " EXCLUDED.volume, EXCLUDED.source)"
        )
        params = [
            (inst_id, c.trade_date, c.open, c.high, c.low, c.close, c.volume, c.oi,
             c.price_basis, c.source)
            for c in candles
        ]  # fmt: skip
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(sql, params)
            return max(cur.rowcount, 0)

    def closes(self, inst_id: int, since: date) -> list[tuple[date, Decimal]]:
        return [
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT trade_date, close FROM candles_daily WHERE instrument_id = %s"
                " AND trade_date >= %s ORDER BY 1",
                (inst_id, since),
            ).fetchall()
        ]

    def gaps(self, inst_id: int) -> list[tuple[int, date, date, str]]:
        rows = self.conn.execute(
            "SELECT id, gap_start, gap_end, status FROM data_gaps"
            " WHERE instrument_id = %s AND interval = '1d' ORDER BY gap_start",
            (inst_id,),
        ).fetchall()
        return [
            (r[0], r[1].astimezone(IST).date(), r[2].astimezone(IST).date(), r[3]) for r in rows
        ]

    def save_gaps(
        self,
        inst_id: int,
        rows: Sequence[tuple[date, date, str]],
        filled: Sequence[int],
        run_id: int | None,
    ) -> None:
        if filled:
            self.conn.execute(
                "UPDATE data_gaps SET status = 'filled', filled_by_run_id = %s WHERE id = ANY(%s)",
                (run_id, list(filled)),
            )
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO data_gaps (instrument_id, interval, gap_start, gap_end, status)"
                    " VALUES (%s, '1d', %s, %s, %s)"
                    " ON CONFLICT (instrument_id, interval, gap_start) DO UPDATE SET"
                    " gap_end = EXCLUDED.gap_end, status = EXCLUDED.status"
                    " WHERE (data_gaps.gap_end, data_gaps.status)"
                    " IS DISTINCT FROM (EXCLUDED.gap_end, EXCLUDED.status)",
                    [(inst_id, _ist_midnight(a), _ist_midnight(b), s) for a, b, s in rows],
                )

    def add_price_events(self, inst_id: int, events: Sequence[tuple[date, Decimal]]) -> int:
        if not events:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO price_events (instrument_id, trade_date, kind, ratio)"
                " VALUES (%s, %s, 'suspected_split_bonus', %s) ON CONFLICT DO NOTHING",
                [(inst_id, d, r) for d, r in events],
            )
            return max(cur.rowcount, 0)

    def gap_counts(self, ids: Sequence[int]) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, count(*) FROM data_gaps WHERE interval = '1d'"
            " AND instrument_id = ANY(%s) GROUP BY status",
            (list(ids),),
        ).fetchall()
        return {status: n for status, n in rows}

    def special_sessions(self) -> list[date]:
        return special_sessions(self.conn)

    def price_event_count(self, ids: Sequence[int]) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM price_events WHERE instrument_id = ANY(%s)", (list(ids),)
        ).fetchone()[0]


# --- special sessions (S1 review answer 2) -----------------------------------------------

SPECIAL_SHARE = 0.5
SPECIAL_SESSIONS_SQL = """
WITH members AS (
    SELECT u.instrument_id, min(c.trade_date) AS first_day
    FROM universe_members u JOIN candles_daily c ON c.instrument_id = u.instrument_id
    WHERE u.universe = %s GROUP BY u.instrument_id
),
days AS (
    SELECT c.trade_date FROM candles_daily c JOIN instruments i ON i.id = c.instrument_id
    WHERE i.instrument_key = %s
)
SELECT d.trade_date FROM days d
JOIN members m ON m.first_day <= d.trade_date
LEFT JOIN candles_daily c ON c.instrument_id = m.instrument_id AND c.trade_date = d.trade_date
GROUP BY d.trade_date
HAVING count(c.instrument_id) < %s * count(*)
ORDER BY d.trade_date
"""


def special_sessions(
    conn: Any, *, universe: str = "nifty500", calendar_key: str = CALENDAR_KEY
) -> list[date]:
    """Calendar (Nifty 50) days on which fewer than 50% of the nifty500 members that already
    had history have a candle: special sessions such as Diwali Muhurat trading or a Budget-day
    Saturday. They are left out of the gap and label calendar."""
    rows = conn.execute(SPECIAL_SESSIONS_SQL, (universe, calendar_key, SPECIAL_SHARE)).fetchall()
    return [r[0] for r in rows]


def delete_special_session_gaps(conn: Any, days: Sequence[date], *, dry_run: bool) -> int:
    """Delete gap rows that lie entirely on special sessions (one-day gaps on those days)."""
    if not days:
        return 0
    where = (
        " WHERE interval = '1d' AND (gap_start AT TIME ZONE 'Asia/Kolkata')::date = ANY(%s)"
        " AND (gap_end AT TIME ZONE 'Asia/Kolkata')::date = (gap_start AT TIME ZONE"
        " 'Asia/Kolkata')::date"
    )
    if dry_run:
        return conn.execute("SELECT count(*) FROM data_gaps" + where, (list(days),)).fetchone()[0]
    return conn.execute("DELETE FROM data_gaps" + where, (list(days),)).rowcount


# --- progress (resume) -------------------------------------------------------------------


class Progress:
    """Instruments finished for one target day: ``{"to": day, "done": {id: from-day}}``."""

    def __init__(self, data_dir: Path, target: date) -> None:
        self.path = data_dir / "state" / PROGRESS_NAME
        self.target = target.isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.done: dict[str, str] = data.get("done", {}) if data.get("to") == self.target else {}

    def is_done(self, inst_id: int, start: date | None) -> bool:
        done_from = self.done.get(str(inst_id))
        return done_from is not None and (start is None or start >= date.fromisoformat(done_from))

    def mark(self, inst_id: int, start: date) -> None:
        self.done[str(inst_id)] = start.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"to": self.target, "done": self.done}), encoding="utf-8")
            os.replace(tmp, self.path)


# --- fetching with fallback --------------------------------------------------------------


@dataclass
class Fetchers:
    """Where candles come from. ``upstox`` is None when no valid token exists."""

    upstox: Any | None  # an UpstoxClient
    yahoo: Callable[[str, date, date], dc.CandleBatch] = dc.fetch_yfinance
    upstox_fetch: Callable[..., dc.CandleBatch] = dc.fetch_upstox
    yahoo_failures_in_a_row: int = 0
    yahoo_off: bool = False  # after YAHOO_GIVE_UP failures in a row (e.g. Yahoo refuses us)

    def yahoo_call(self, symbol: str, start: date, end: date) -> dc.CandleBatch:
        if self.yahoo_off:
            raise dc.CandleSourceError("yfinance switched off for this run (repeated failures)")
        try:
            batch = self.yahoo(symbol, start, end)
        except dc.CandleSourceError:
            self.yahoo_failures_in_a_row += 1
            if self.yahoo_failures_in_a_row >= YAHOO_GIVE_UP:
                self.yahoo_off = True
                log.warning("yfinance_switched_off", failures=self.yahoo_failures_in_a_row)
            raise
        self.yahoo_failures_in_a_row = 0
        return batch


@dataclass
class InstResult:
    rows: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    requests: Counter[str] = field(default_factory=Counter)
    tried: set[str] = field(default_factory=set)
    failed_sources: set[str] = field(default_factory=set)
    error: str | None = None
    unsupported: bool = False
    malformed: int = 0
    duplicates: int = 0
    events: int = 0


@dataclass
class DailySummary:
    universe: str
    dry_run: bool = False
    status: str = "success"
    run_id: int | None = None
    instruments: int = 0
    done: int = 0
    resumed: int = 0
    failed: int = 0
    unsupported: int = 0
    duplicates: int = 0
    rows: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    requests: Counter[str] = field(default_factory=Counter)
    malformed: int = 0
    gaps: dict[str, int] = field(default_factory=dict)
    events_new: int = 0
    events_total: int = 0
    seconds: float = 0.0
    token: str = "valid"
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        g = self.gaps
        return [
            f"{prefix}daily candles ({self.universe}): {self.status}; instruments"
            f" {self.instruments} (done {self.done}, already done {self.resumed},"
            f" not served by Upstox {self.unsupported}, failed {self.failed}); rows {self.rows}",
            f"  requests: upstox {self.requests['upstox']}, yfinance {self.requests['yfinance']};"
            f" duration {self.seconds:.0f} s; Upstox token {self.token}",
            f"  rows by source: upstox {self.by_source['upstox']},"
            f" yfinance {self.by_source['yfinance']}; malformed skipped {self.malformed},"
            f" duplicate days dropped {self.duplicates}",
            f"  gaps: open {g.get('open', 0)}, filled {g.get('filled', 0)},"
            f" unfillable {g.get('unfillable', 0)}; price events: new {self.events_new},"
            f" total {self.events_total}",
        ]


class Loader:
    def __init__(
        self,
        store: Store,
        fetchers: Fetchers,
        *,
        target: date,
        dry_run: bool = False,
        run_id: int | None = None,
    ) -> None:
        self.store, self.f, self.target = store, fetchers, target
        self.dry_run, self.run_id = dry_run, run_id
        self.calendar: list[date] = []
        self.market: dict[date, Decimal] = {}
        self.calendar_id: int | None = None

    def load_calendar(self) -> None:
        if self.calendar_id is None:
            return
        closes = self.store.closes(self.calendar_id, date(1990, 1, 1))
        # special sessions aren't part of the gap calendar (stores without them: none)
        special = set(getattr(self.store, "special_sessions", lambda: [])())
        self.calendar = [d for d, _ in closes if d not in special]
        self.market = dict(closes)

    def _fetch(self, inst: Inst, start: date, end: date, res: InstResult) -> list[DailyCandle]:
        """Upstox first; for stocks fall back to yfinance. Raises on total failure."""
        errors = []
        if self.f.upstox is not None and inst.instrument_key:
            res.tried.add("upstox")
            try:
                batch = self.f.upstox_fetch(
                    self.f.upstox, inst.instrument_key, inst.symbol, start, end
                )
                res.requests["upstox"] += batch.requests
                res.malformed += batch.malformed
                res.duplicates += batch.duplicates
                return batch.candles
            except upstox.TokenExpired as exc:
                self.f.upstox = None  # no more Upstox calls in this run
                errors.append(str(exc))
                res.failed_sources.add("upstox")
            except (upstox.UpstoxError, dc.CandleSourceError) as exc:
                res.requests["upstox"] += 1
                errors.append(mask_text(str(exc)))
                if any(code in str(exc) for code in UNSUPPORTED_CODES):
                    res.unsupported = True  # not served by Upstox; not a source failure
                else:
                    res.failed_sources.add("upstox")
        elif inst.is_index:
            errors.append("no valid Upstox token (indices load from Upstox only)")
        if not inst.is_index:
            res.tried.add("yfinance")
            try:
                batch = self.f.yahoo_call(inst.symbol, start, end)
                res.requests["yfinance"] += batch.requests
                res.malformed += batch.malformed
                return batch.candles
            except dc.CandleSourceError as exc:
                res.requests["yfinance"] += 1
                errors.append(mask_text(str(exc)))
                res.failed_sources.add("yfinance")
        raise dc.CandleSourceError("; ".join(errors) or "no source available")

    def _yahoo_days(self, inst: Inst, days: Sequence[date], res: InstResult) -> list[DailyCandle]:
        """ONE yfinance request spanning ``days``; only candles of those days are kept."""
        res.tried.add("yfinance")
        try:
            batch = self.f.yahoo_call(inst.symbol, min(days), max(days))
        except dc.CandleSourceError:
            res.requests["yfinance"] += 1
            res.failed_sources.add("yfinance")
            return []
        res.requests["yfinance"] += batch.requests
        wanted = set(days)
        return [c for c in batch.candles if c.trade_date in wanted]

    def _store(self, inst: Inst, candles: list[DailyCandle], res: InstResult) -> None:
        for c in candles:
            res.by_source[c.source] += 1
        if not self.dry_run:
            res.rows += self.store.upsert(inst.id, candles)

    def process(self, inst: Inst, start_from: date | None) -> InstResult:
        """Load one instrument: new days, earlier history if asked, open gaps; then gaps and
        price events."""
        res = InstResult()
        have = self.store.stored_dates(inst.id)
        wanted: list[tuple[date, date]] = []
        if have:
            if have[-1] < self.target:
                wanted.append((have[-1] + timedelta(days=1), self.target))
            if start_from and start_from < have[0]:
                wanted.append((start_from, have[0] - timedelta(days=1)))
        else:
            wanted.append((start_from or DEFAULT_FROM, self.target))
        old_gaps = self.store.gaps(inst.id)
        wanted += [(a, b) for _, a, b, status in old_gaps if status == "open"]
        if self.f.upstox is not None and not inst.is_index:
            # days stored from the yfinance backup: ask Upstox again (upstox replaces them)
            backup_days = self.store.source_dates(inst.id, "yfinance")
            if backup_days:
                wanted.append((backup_days[0], backup_days[-1]))
        fetched_ok = False
        for start, end in wanted:
            try:
                self._store(inst, self._fetch(inst, start, end, res), res)
                fetched_ok = True
            except dc.CandleSourceError as exc:
                res.error = str(exc)
        if res.error and not fetched_ok:
            return res
        if self.dry_run or inst.id == self.calendar_id:
            return res
        self._gaps_and_events(inst, have, old_gaps, res)
        res.error = None if fetched_ok else res.error
        return res

    def _gaps_and_events(self, inst, have_before, old_gaps, res: InstResult) -> None:
        unfillable_days = {
            d for _, a, b, s in old_gaps if s == "unfillable" for d in self.calendar if a <= d <= b
        }
        have = self.store.stored_dates(inst.id)
        missing = [
            d for d in missing_days(have, self.calendar, self.target) if d not in unfillable_days
        ]
        # retry new holes with the other source (stocks fetched from Upstox -> yfinance)
        if missing and not inst.is_index and "upstox" in res.tried and "yfinance" not in res.tried:
            self._store(inst, self._yahoo_days(inst, missing, res), res)
            have = self.store.stored_dates(inst.id)
            missing = [
                d
                for d in missing_days(have, self.calendar, self.target)
                if d not in unfillable_days
            ]
        all_sources = {"upstox"} if inst.is_index else {"upstox", "yfinance"}
        exhausted = all_sources <= res.tried
        status = "unfillable" if exhausted else "open"
        new_rows = [(a, b, status) for a, b in ranges(missing, self.calendar)]
        present = set(have)
        filled = [
            gid for gid, a, b, s in old_gaps
            if s == "open" and all(d in present for d in self.calendar if a <= d <= b)
        ]  # fmt: skip
        self.store.save_gaps(inst.id, new_rows, filled, self.run_id)
        if not inst.is_index and have:
            since = (min(have_before[-1], have[-1]) if have_before else have[0]) - timedelta(days=7)
            events = suspected_splits(self.store.closes(inst.id, since), self.market)
            res.events = self.store.add_price_events(inst.id, events)


# --- the command -------------------------------------------------------------------------


class AllFailed(Exception):
    def __init__(self, message: str, summary: DailySummary) -> None:
        super().__init__(message)
        self.summary = summary


def run_daily(
    settings: Settings,
    *,
    universe: str = "all",
    start_from: date | None = None,
    symbol: str | None = None,
    dry_run: bool = False,
    store: Store | None = None,
    fetchers: Fetchers | None = None,
    connect: Callable[..., Any] | None = None,
    health: SourceHealth | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DailySummary:
    """The ``candles daily`` command (see the module docstring)."""
    if universe not in UNIVERSES:
        raise ConfigError(f"unknown universe {universe!r}; use one of {', '.join(UNIVERSES)}")
    started = time_mod.monotonic()
    summary = DailySummary(universe=symbol or universe, dry_run=dry_run)
    target = last_trading_day(now())
    if fetchers is None:
        fetchers = Fetchers(upstox=_upstox_client(settings, summary))
    elif fetchers.upstox is None:
        summary.token = "missing or expired (stocks from yfinance; indices skipped)"
    if store is not None:
        return _run(settings, summary, store, fetchers, target, universe, start_from, symbol,
                    dry_run, connect, health, started)  # fmt: skip
    with (connect or postgres.connect)(settings.require("database")) as conn:
        return _run(settings, summary, PgStore(conn), fetchers, target, universe, start_from,
                    symbol, dry_run, connect, health, started)  # fmt: skip


def _upstox_client(settings: Settings, summary: DailySummary) -> Any | None:
    if settings.app_profile == "restricted" or settings.missing("upstox"):
        summary.token = "not set up (stocks from yfinance; indices skipped)"
        return None
    try:
        return upstox.UpstoxClient(settings)
    except upstox.TokenExpired:
        summary.token = "missing or expired (stocks from yfinance; indices skipped)"
        log.warning("candles_without_upstox_token")
        return None


def _run(settings, summary, store, fetchers, target, universe, start_from, symbol, dry_run,
         connect, health, started) -> DailySummary:  # fmt: skip
    targets = store.instruments(None if symbol else universe, symbol)
    if symbol and not targets:
        raise ConfigError(f"{symbol} is not an active NSE instrument")
    calendar_inst = store.calendar_instrument()
    if calendar_inst and all(t.id != calendar_inst.id for t in targets):
        targets = [calendar_inst, *targets]  # the calendar is always loaded first
    elif calendar_inst:
        targets = [calendar_inst, *[t for t in targets if t.id != calendar_inst.id]]
    summary.instruments = len(targets)
    progress = Progress(settings.data_dir, target)
    health = None if dry_run else (health or SourceHealth(settings.data_dir))

    def body(run_id: int | None) -> None:
        loader = Loader(store, fetchers, target=target, dry_run=dry_run, run_id=run_id)
        loader.calendar_id = calendar_inst.id if calendar_inst else None
        per_source = {"upstox": Counter(), "yfinance": Counter()}
        for inst in targets:
            if (
                not dry_run
                and progress.is_done(inst.id, start_from)
                and inst.id != loader.calendar_id
            ):
                summary.resumed += 1
                continue
            res = loader.process(inst, start_from)
            if inst.id == loader.calendar_id:
                loader.load_calendar()
            summary.rows += res.rows
            summary.by_source.update(res.by_source)
            summary.requests.update(res.requests)
            summary.malformed += res.malformed
            summary.duplicates += res.duplicates
            summary.events_new += res.events
            for src in res.tried:
                per_source[src]["failed" if src in res.failed_sources else "ok"] += 1
            if res.error and res.unsupported and not res.by_source:
                summary.unsupported += 1
                log.info("candles_not_served_by_upstox", symbol=inst.symbol)
                if not dry_run:
                    progress.mark(inst.id, start_from or DEFAULT_FROM)
                continue
            if res.error:
                summary.failed += 1
                summary.errors.append(f"{inst.symbol}: {res.error}")
                log.warning("candles_instrument_failed", symbol=inst.symbol, error=res.error)
                continue
            summary.done += 1
            if not dry_run:
                progress.mark(inst.id, start_from or DEFAULT_FROM)
        if health is not None:
            for src, name in (("upstox", UPSTOX_HEALTH), ("yfinance", YAHOO_HEALTH)):
                ok, bad = per_source[src]["ok"], per_source[src]["failed"]
                if ok:
                    health.record_content(name, items=ok, malformed=bad, without_time=0)
                elif bad:
                    last = next((e for e in reversed(summary.errors)), f"{src} failed")
                    health.record_failure(name, last)
        ids = [t.id for t in targets]
        summary.gaps = store.gap_counts(ids)
        summary.events_total = store.price_event_count(ids)

    if dry_run:
        body(None)
    else:
        params = {"universe": universe, "from": start_from.isoformat() if start_from else None,
                  "symbol": symbol}  # fmt: skip
        with ingestion_run(settings, JOB, source="upstox", params=params, connect=connect) as run:
            summary.run_id = run.id
            body(run.id)
            run.add_rows(summary.rows)
            attempted = summary.done + summary.failed
            if summary.failed and summary.failed == attempted:
                summary.status = "failed"
                summary.seconds = time_mod.monotonic() - started
                raise AllFailed(f"all {attempted} instruments failed", summary)
            if summary.failed:
                summary.status = "partial"
                run.mark_partial(
                    f"{summary.failed} of {attempted} instruments failed: "
                    + "; ".join(summary.errors[:5])
                )
    summary.status = "partial" if summary.failed else summary.status
    summary.seconds = time_mod.monotonic() - started
    log.info("candles_loaded", universe=summary.universe, rows=summary.rows,
             done=summary.done, failed=summary.failed, status=summary.status)  # fmt: skip
    return summary


# --- entry point -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.candles``."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.candles")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="load daily candles (Upstox, yfinance backup)")
    daily.add_argument("--universe", choices=UNIVERSES, default="all")
    daily.add_argument("--from", dest="start_from", type=date.fromisoformat,
                       help="also load history from this day (YYYY-MM-DD)")  # fmt: skip
    daily.add_argument("--symbol", help="only this instrument (NSE symbol)")
    daily.add_argument("--dry-run", action="store_true", help="fetch, save nothing")
    special = sub.add_parser(
        "special-sessions", help="list special sessions and delete their gap rows"
    )
    special.add_argument("--dry-run", action="store_true", help="count only, delete nothing")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            if args.command == "special-sessions":
                with postgres.connect(settings.require("database")) as conn:
                    days = special_sessions(conn)
                    gaps = delete_special_session_gaps(conn, days, dry_run=args.dry_run)
                prefix = "DRY RUN (nothing deleted): " if args.dry_run else ""
                print(f"{prefix}special sessions: {len(days)} days; gap rows on them"
                      f" {'to delete' if args.dry_run else 'deleted'}: {gaps}")  # fmt: skip
                for day in days:
                    print(f"  {day.isoformat()} ({day.strftime('%a')})")
                return 0
            summary = run_daily(
                settings, universe=args.universe, start_from=args.start_from,
                symbol=args.symbol, dry_run=args.dry_run,
            )  # fmt: skip
            print("\n".join(summary.lines()))
        except AllFailed as exc:
            print("\n".join(exc.summary.lines()))
            print(f"Failed: {exc}", file=sys.stderr)
            return 1
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
