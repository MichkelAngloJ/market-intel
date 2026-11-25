"""1-minute candles from Upstox into Postgres ``candles_intraday`` (sprint S4i; decision
2026-11-25).

    uv run python -m market_intel.candles intraday --universe nifty50 [--from 2022-01-01]
        [--symbol S] [--dry-run]

- **Instruments:** the universe's members (default nifty50), Nifty 50, India VIX and the NSE
  sector indices that match the members' sectors (:data:`SECTOR_INDEX`, chosen by name).
- **Source:** Upstox V3 historical candles, ``minutes/1`` (from Jan 2022; at most one month per
  request for 1-15 minute intervals; checked 2026-11-24). Upstox only: there is no yfinance
  backup for intraday. Records are raw prices, ``source = "upstox"``.
- **Regular session only:** bars starting 09:15-15:29 IST are kept; pre-open, after-hours and
  special-session bars are dropped and counted. Rows breaking the OHLC rules are dropped and
  counted.
- **Thin days:** a day with fewer than 300 bars for an instrument gets a ``data_gaps`` row
  (interval ``1m``, status ``unfillable``, note "thin day: N bars").
- **Storage:** each (instrument, month) is COPY-ed into a temporary table and upserted on the
  primary key; unchanged rows aren't rewritten (a rerun writes 0).
- **Resumable and incremental:** ``DATA_DIR/state/intraday_progress.json`` records the complete
  months per instrument; each instrument continues from its last stored day.
- One ``ingestion_run(job="candles_intraday", source="upstox")`` (partial on some failures);
  source health ``upstox_intraday``.
"""

from __future__ import annotations

import json
import time as time_mod
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

from market_intel import upstox
from market_intel.config import ConfigError, Settings
from market_intel.file_lock import file_lock, safe_replace
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import get_logger, mask_text
from market_intel.source_health import SourceHealth

IST = ZoneInfo("Asia/Kolkata")
JOB = "candles_intraday"
HEALTH = "upstox_intraday"
UPSTOX_FROM = date(2022, 1, 1)
SESSION_START, SESSION_LAST = time(9, 15), time(15, 29)  # bar starts kept (IST)
THIN_DAY_BARS = 300
NIFTY_KEY, VIX_KEY = "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"
PROGRESS_NAME = "intraday_progress.json"
# The NSE sector index for each NSE industry (sector) name, chosen by name. Sectors without an
# index of their own (Telecommunication, Capital Goods, Consumer Services) have none.
SECTOR_INDEX: dict[str, str] = {
    "Financial Services": "NSE_INDEX|Nifty Fin Service",
    "Automobile and Auto Components": "NSE_INDEX|Nifty Auto",
    "Healthcare": "NSE_INDEX|NIFTY HEALTHCARE",
    "Metals & Mining": "NSE_INDEX|Nifty Metal",
    "Information Technology": "NSE_INDEX|Nifty IT",
    "Fast Moving Consumer Goods": "NSE_INDEX|Nifty FMCG",
    "Oil Gas & Consumable Fuels": "NSE_INDEX|NIFTY OIL AND GAS",
    "Construction Materials": "NSE_INDEX|Nifty Cement",
    "Consumer Durables": "NSE_INDEX|NIFTY CONSR DURBL",
    "Services": "NSE_INDEX|Nifty Serv Sector",
    "Power": "NSE_INDEX|Nifty Energy",  # closest: no power-only index
    "Construction": "NSE_INDEX|Nifty Infra",  # closest: no construction-only index
}

log = get_logger("market_intel.intraday_candles")


@dataclass(frozen=True)
class Target:
    id: int
    symbol: str
    instrument_key: str
    role: str  # "stock", "market", "vix" or "sector"


def months(start: date, end: date) -> Iterator[tuple[date, date]]:
    """(first, last) day of each calendar month touching start..end, clipped to it."""
    first = date(start.year, start.month, 1)
    while first <= end:
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        yield max(first, start), min(nxt - timedelta(days=1), end)
        first = nxt


# --- parsing -----------------------------------------------------------------------------


@dataclass
class MinuteBatch:
    frame: pd.DataFrame  # ts (UTC), open, high, low, close, volume, oi
    dropped_session: int = 0
    dropped_invalid: int = 0


def parse_minutes(payload: Any, special: set[date] = frozenset()) -> MinuteBatch:
    """Upstox V3 minute candles -> regular-session bars (vectorised checks, no per-row objects)."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise upstox.UpstoxError("unexpected Upstox response (status is not success)")
    rows = (payload.get("data") or {}).get("candles")
    if not isinstance(rows, list):
        raise upstox.UpstoxError("unexpected Upstox response (no candles list)")
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    if not rows:
        return MinuteBatch(empty)
    df = pd.DataFrame(
        [r[:7] if len(r) >= 7 else [*r[:6], 0] for r in rows],
        columns=["ts", "open", "high", "low", "close", "volume", "oi"],
    )
    local = pd.to_datetime(df["ts"], errors="coerce", utc=True).dt.tz_convert(IST)
    for col in ("open", "high", "low", "close", "volume", "oi"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    t = local.dt.time
    in_session = local.notna() & (t >= SESSION_START) & (t <= SESSION_LAST)
    if special:
        in_session &= ~local.dt.date.isin(special)
    batch = MinuteBatch(empty, dropped_session=int((~in_session & local.notna()).sum()))
    df, local = df[in_session], local[in_session]
    prices = df[["open", "high", "low", "close"]].round(4)
    valid = (
        prices.notna().all(axis=1)
        & (prices.min(axis=1) >= 0)
        & (prices["low"] <= prices[["open", "close"]].min(axis=1))
        & (prices["high"] >= prices[["open", "close"]].max(axis=1))
        & (df["volume"].fillna(0) >= 0)
        & local.notna()
    )
    batch.dropped_invalid = int((~valid).sum())
    out = prices[valid].copy()
    out.insert(0, "ts", local[valid].dt.tz_convert(UTC))
    out["volume"] = df.loc[valid, "volume"].fillna(0).astype("int64")
    out["oi"] = df.loc[valid, "oi"].fillna(0).astype("int64")
    batch.frame = out.drop_duplicates("ts", keep="first").sort_values("ts", ignore_index=True)
    return batch


def thin_days(frame: pd.DataFrame, threshold: int = THIN_DAY_BARS) -> dict[date, int]:
    """Days with fewer than ``threshold`` regular-session bars: {day: bars}."""
    if frame.empty:
        return {}
    days = frame["ts"].dt.tz_convert(IST).dt.date
    counts = days.value_counts()
    return {d: int(n) for d, n in counts.items() if n < threshold}


# --- storage -----------------------------------------------------------------------------


class IntradayStore:
    """SQL for the loader (app login)."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def targets(self, universe: str, symbol: str | None) -> list[Target]:
        members = self.conn.execute(
            "SELECT i.id, i.symbol, i.instrument_key, i.sector FROM instruments i"
            " JOIN universe_members u ON u.instrument_id = i.id AND u.universe = %s"
            " WHERE i.is_active AND i.instrument_key IS NOT NULL ORDER BY i.symbol",
            (universe,),
        ).fetchall()
        sector_keys = sorted({SECTOR_INDEX[m[3]] for m in members if m[3] in SECTOR_INDEX})
        index_keys = [NIFTY_KEY, VIX_KEY, *sector_keys]
        indices = {
            r[2]: Target(r[0], r[1], r[2], "")
            for r in self.conn.execute(
                "SELECT id, symbol, instrument_key FROM instruments WHERE instrument_key = ANY(%s)",
                (index_keys,),
            ).fetchall()
        }
        out = [
            Target(
                k_.id,
                k_.symbol,
                k_.instrument_key,
                "market" if key == NIFTY_KEY else "vix" if key == VIX_KEY else "sector",
            )
            for key in index_keys
            if (k_ := indices.get(key))
        ]
        out += [Target(m[0], m[1], m[2], "stock") for m in members]
        if symbol:
            out = [t for t in out if t.symbol == symbol]
        return out

    def last_day(self, inst_id: int) -> date | None:
        row = self.conn.execute(
            "SELECT max(ts) FROM candles_intraday WHERE instrument_id = %s AND interval = '1m'",
            (inst_id,),
        ).fetchone()
        return row[0].astimezone(IST).date() if row and row[0] else None

    def upsert(self, inst_id: int, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        with self.conn.transaction():
            self.conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS tmp_intraday (ts timestamptz, open numeric,"
                " high numeric, low numeric, close numeric, volume bigint, oi bigint)"
                " ON COMMIT DELETE ROWS"
            )
            # also when nested in an outer transaction (a savepoint does not trigger ON COMMIT)
            self.conn.execute("TRUNCATE tmp_intraday")
            with (
                self.conn.cursor() as cur,
                cur.copy(
                    "COPY tmp_intraday (ts, open, high, low, close, volume, oi) FROM STDIN"
                ) as copy,
            ):
                for row in frame.itertuples(index=False, name=None):
                    copy.write_row(row)
            return self.conn.execute(
                "INSERT INTO candles_intraday (instrument_id, ts, interval, open, high, low, close,"
                " volume, oi, price_basis, source)"
                " SELECT %s, ts, '1m', open, high, low, close, volume, oi, 'raw', 'upstox'"
                " FROM tmp_intraday"
                " ON CONFLICT (instrument_id, interval, ts) DO UPDATE SET open = EXCLUDED.open,"
                " high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,"
                " volume = EXCLUDED.volume, oi = EXCLUDED.oi, ingested_at = now()"
                " WHERE (candles_intraday.open, candles_intraday.high, candles_intraday.low,"
                " candles_intraday.close, candles_intraday.volume) IS DISTINCT FROM"
                " (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)",
                (inst_id,),
            ).rowcount

    def save_thin_days(self, inst_id: int, days: dict[date, int]) -> None:
        if not days:
            return
        rows = [
            (
                inst_id,
                datetime.combine(d, SESSION_START, IST),
                datetime.combine(d, SESSION_LAST, IST),
                f"thin day: {n} bars",
            )
            for d, n in days.items()
        ]
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO data_gaps (instrument_id, interval, gap_start, gap_end, status, note)"
                " VALUES (%s, '1m', %s, %s, 'unfillable', %s)"
                " ON CONFLICT (instrument_id, interval, gap_start) DO UPDATE SET"
                " gap_end = EXCLUDED.gap_end, note = EXCLUDED.note, status = EXCLUDED.status",
                rows,
            )

    def thin_day_count(self, ids: Sequence[int]) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM data_gaps WHERE interval = '1m' AND instrument_id = ANY(%s)",
            (list(ids),),
        ).fetchone()[0]


class Progress:
    """Complete months per instrument: {"done": {"<id>": ["YYYY-MM", ...]}}."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state" / PROGRESS_NAME
        try:
            self.done: dict[str, list[str]] = json.loads(self.path.read_text(encoding="utf-8"))[
                "done"
            ]
        except (OSError, ValueError, KeyError):
            self.done = {}

    def has(self, inst_id: int, month: date) -> bool:
        return f"{month:%Y-%m}" in self.done.get(str(inst_id), [])

    def mark(self, inst_id: int, month: date) -> None:
        self.done.setdefault(str(inst_id), []).append(f"{month:%Y-%m}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"done": self.done}), encoding="utf-8")
            safe_replace(tmp, self.path)


# --- the command -------------------------------------------------------------------------


@dataclass
class IntradaySummary:
    universe: str
    dry_run: bool = False
    status: str = "success"
    run_id: int | None = None
    instruments: list[str] = field(default_factory=list)
    sector_indices: list[str] = field(default_factory=list)
    done: int = 0
    failed: int = 0
    rows: int = 0
    bars: int = 0
    requests: int = 0
    months_skipped: int = 0
    dropped_session: int = 0
    dropped_invalid: int = 0
    thin_days_new: int = 0
    thin_days_total: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        return [
            f"{prefix}intraday candles ({self.universe}): {self.status}; instruments"
            f" {len(self.instruments)} (done {self.done}, failed {self.failed}); rows written"
            f" {self.rows:,} of {self.bars:,} bars fetched",
            f"  requests {self.requests:,} (months already complete: {self.months_skipped:,});"
            f" duration {self.seconds:.0f} s",
            f"  dropped: out of session {self.dropped_session:,}, invalid OHLC"
            f" {self.dropped_invalid:,}; thin days (< {THIN_DAY_BARS} bars): new"
            f" {self.thin_days_new:,}, total {self.thin_days_total:,}",
            f"  sector indices: {', '.join(self.sector_indices) or 'none'}",
            *[f"  failed: {e}" for e in self.errors[:10]],
        ]


def last_trading_day(now: datetime) -> date:
    from market_intel.candles import last_trading_day as daily_last

    return daily_last(now)


def run_intraday(
    settings: Settings,
    conn: Any,
    *,
    universe: str = "nifty50",
    start: date = UPSTOX_FROM,
    symbol: str | None = None,
    dry_run: bool = False,
    client: Any | None = None,
    health: SourceHealth | None = None,
    connect: Callable[..., Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IntradaySummary:
    """The ``candles intraday`` command (see the module docstring)."""
    from market_intel.candles import special_sessions

    t0 = time_mod.monotonic()
    store = IntradayStore(conn)
    summary = IntradaySummary(universe=symbol or universe, dry_run=dry_run)
    targets = store.targets(universe, symbol)
    if not targets:
        raise ConfigError(
            f"no instruments for universe {universe!r}"
            + (f" and symbol {symbol}" if symbol else "")
        )
    summary.instruments = [t.symbol for t in targets]
    summary.sector_indices = [
        t.instrument_key.split("|", 1)[1] for t in targets if t.role == "sector"
    ]
    client = client or upstox.UpstoxClient(settings)  # TokenExpired without a valid token
    special = set(special_sessions(conn))
    end = last_trading_day(now())
    progress = Progress(settings.data_dir)
    current_month = date(end.year, end.month, 1)

    def body(run: Any | None) -> None:
        for target in targets:
            last = store.last_day(target.id)
            begin = (
                max(start, UPSTOX_FROM) if last is None else max(start, last + timedelta(days=1))
            )
            ok = True
            for first, last_day in months(begin, end):
                month = date(first.year, first.month, 1)
                if not dry_run and progress.has(target.id, month) and month < current_month:
                    summary.months_skipped += 1
                    continue
                try:
                    payload = client.get(
                        f"/v3/historical-candle/{quote(target.instrument_key, safe='')}"
                        f"/minutes/1/{last_day.isoformat()}/{first.isoformat()}"
                    )
                    summary.requests += 1
                    batch = parse_minutes(payload, special)
                except upstox.TokenExpired:
                    raise
                except upstox.UpstoxError as exc:
                    summary.requests += 1
                    ok = False
                    summary.errors.append(f"{target.symbol} {month:%Y-%m}: {mask_text(str(exc))}")
                    log.warning(
                        "intraday_month_failed", symbol=target.symbol, month=f"{month:%Y-%m}"
                    )
                    continue
                summary.bars += len(batch.frame)
                summary.dropped_session += batch.dropped_session
                summary.dropped_invalid += batch.dropped_invalid
                if dry_run:
                    continue
                summary.rows += store.upsert(target.id, batch.frame)
                thin = thin_days(batch.frame)
                store.save_thin_days(target.id, thin)
                summary.thin_days_new += len(thin)
                if month < current_month:  # a past month is fetched to its last day
                    progress.mark(target.id, month)
            if ok:
                summary.done += 1
            else:
                summary.failed += 1
        summary.thin_days_total = store.thin_day_count([t.id for t in targets])
        if health is not None:
            if summary.done:
                health.record_content(
                    HEALTH, items=summary.done, malformed=summary.failed, without_time=0
                )
            elif summary.failed:
                health.record_failure(HEALTH, summary.errors[-1] if summary.errors else "failed")

    if dry_run:
        body(None)
    else:
        health = health or SourceHealth(settings.data_dir)
        params = {"universe": universe, "from": start.isoformat(), "symbol": symbol}
        with ingestion_run(settings, JOB, source="upstox", params=params, connect=connect) as run:
            summary.run_id = run.id
            body(run)
            run.add_rows(summary.rows)
            if summary.failed:
                attempted = summary.done + summary.failed
                summary.status = "failed" if summary.failed == attempted else "partial"
                if summary.status == "partial":
                    run.mark_partial(
                        f"{summary.failed} of {attempted} instruments had failed months: "
                        + "; ".join(summary.errors[:5])
                    )
                else:
                    raise upstox.UpstoxError(f"all {attempted} instruments failed")
    summary.seconds = time_mod.monotonic() - t0
    log.info(
        "intraday_loaded",
        rows=summary.rows,
        requests=summary.requests,
        done=summary.done,
        failed=summary.failed,
        status=summary.status,
    )
    return summary


def counts_by_role(targets: Sequence[Target]) -> Counter[str]:
    return Counter(t.role for t in targets)
