"""Features v1, forward labels and the leakage test (sprint S2; decisions 2026-11-24).

    uv run python -m market_intel.features build [--universe nifty500] [--from DATE] [--version v1]
    uv run python -m market_intel.features labels
    uv run python -m market_intel.features check-leakage [--sample N]

TIMING CONVENTION (enforced here and by the leakage test):

- The features for trading day t use ONLY candles up to and including t's close.
- prediction_time(t) = 08:30 IST on the next weekday after t (a holiday calendar isn't
  available; this can only make the cutoff earlier, never later). Entry is at open(t+1).
- News features use only items with available_at <= prediction_time(t).

Regular sessions only: special sessions (see :func:`market_intel.candles.special_sessions`) are
removed from every candle series before anything is computed, and are not label days.

Features: :data:`market_intel.feature_spec.FEATURES_V1`, computed by ONE pure function,
:func:`build_features`, from DataFrames (it never reads the database, so it can never read
labels). Rolling windows only look back. News counts are stored but marked not for training.

Labels (a separate table): fwd_return(h) = close(t+h) / open(t+1) - 1 over h regular trading
days, h in 21, 63, 126; up = fwd_return > 0. NULL with excluded_reason when the window
(t, t+h] runs past the data (beyond_data), contains a special session, a suspected split/bonus
(price_event) or a calendar day without a candle (gap).
"""

from __future__ import annotations

import argparse
import inspect
import random
import sys
import time as time_mod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import mongo, postgres
from market_intel.feature_spec import (
    EXCLUDED_REASONS,
    FEATURE_NAMES,
    FEATURE_VERSION,
    FORBIDDEN_FEATURE_WORDS,
    HORIZONS,
    LABEL_COLUMNS,
)
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id
from market_intel.mongo import access

COMPONENT = "features"
JOB_FEATURES, JOB_LABELS = "features_build", "labels_build"
IST = ZoneInfo("Asia/Kolkata")
PREDICTION_TIME_IST = time(8, 30)
NIFTY_KEY, VIX_KEY = "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"
OHLCV = ("open", "high", "low", "close", "volume")

log = get_logger("market_intel.features")


# --- timing ------------------------------------------------------------------------------


def prediction_time(day: date) -> datetime:
    """08:30 IST on the next weekday after ``day`` (UTC)."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return datetime.combine(nxt, PREDICTION_TIME_IST, IST).astimezone(UTC)


# --- pure building blocks ----------------------------------------------------------------


def _log_ret(close: pd.Series, k: int) -> pd.Series:
    return np.log(close / close.shift(k))


def returns_20d(close: pd.Series) -> pd.Series:
    """The 20-day log return, shared by ret_20d and the sector average."""
    return _log_ret(close, 20)


def sector_frame(member_closes: dict[int, pd.Series]) -> pd.DataFrame:
    """Equal-weight sector ret_20d by date from the members' own close series."""
    if not member_closes:
        return pd.DataFrame(columns=["sector_r20"], dtype=float)
    r20 = pd.concat({k: returns_20d(v) for k, v in member_closes.items()}, axis=1)
    return pd.DataFrame({"sector_r20": r20.mean(axis=1, skipna=True)})


def market_frame(nifty: pd.DataFrame | None, vix: pd.DataFrame | None) -> pd.DataFrame:
    """Market inputs by date: Nifty 50 r5, r20, close/SMA200-1; India VIX level and 5-day
    change, each computed on its own series (looking back only)."""
    parts = {}
    if nifty is not None and len(nifty):
        c = nifty["close"].astype(float)
        parts["mkt_ret_5d"] = _log_ret(c, 5)
        parts["mkt_ret_20d"] = _log_ret(c, 20)
        parts["mkt_close_sma200"] = c / c.rolling(200, min_periods=200).mean() - 1
    if vix is not None and len(vix):
        v = vix["close"].astype(float)
        parts["vix_level"] = v
        parts["vix_chg_5d"] = v / v.shift(5) - 1
    frame = pd.DataFrame(parts)
    for col in ("mkt_ret_5d", "mkt_ret_20d", "mkt_close_sma200", "vix_level", "vix_chg_5d"):
        if col not in frame:
            frame[col] = np.nan
    return frame


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    return rsi.where(loss != 0, 100.0).where(gain.notna())


@dataclass(frozen=True)
class NewsItem:
    kind: str  # "exchange" or "company_news"
    available_at: datetime


def _news_counts(days: pd.Index, items: Sequence[NewsItem], kind: str, windows: int) -> np.ndarray:
    """Items of ``kind`` with available_at in (prediction_time(t - windows rows),
    prediction_time(t)]."""
    cutoffs = np.array([prediction_time(d).timestamp() for d in days])
    times = np.sort([i.available_at.timestamp() for i in items if i.kind == kind])
    upto = np.searchsorted(times, cutoffs, side="right")
    start = (
        np.concatenate([np.full(windows, -np.inf), cutoffs[:-windows]])
        if len(cutoffs) > windows
        else np.full(len(cutoffs), -np.inf)
    )
    start = start[: len(cutoffs)]
    since = np.searchsorted(times, start, side="right")
    return (upto - since).astype(float)


def build_features(
    candles: pd.DataFrame,
    market: pd.DataFrame,
    sector: pd.DataFrame,
    *,
    events: Iterable[date] = (),
    news: Sequence[NewsItem] = (),
) -> pd.DataFrame:
    """Features v1 for ONE instrument. Pure: DataFrames in, DataFrame out; never reads the
    database (so never the labels).

    ``candles``: the instrument's regular-session candles indexed by trade_date (sorted), with
    open, high, low, close, volume. ``market``: :func:`market_frame` by date. ``sector``:
    :func:`sector_frame` by date. ``events``: suspected split/bonus days. ``news``: the
    instrument's news items. Every rolling window looks back only.
    """
    c = candles.sort_index()
    close, high, low, opn = (c[k].astype(float) for k in ("close", "high", "low", "open"))
    volume = c["volume"].astype(float)
    out = pd.DataFrame(index=c.index)
    for k in (1, 5, 20, 60, 120):
        out[f"ret_{k}d"] = _log_ret(close, k)
    daily = _log_ret(close, 1)
    out["vol_20d"] = daily.rolling(20, min_periods=20).std()
    out["vol_60d"] = daily.rolling(60, min_periods=60).std()
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(
        axis=1
    )
    out["atr14_pct"] = true_range.rolling(14, min_periods=14).mean() / close
    sma = {n: close.rolling(n, min_periods=n).mean() for n in (20, 50, 200)}
    out["close_sma20"] = close / sma[20] - 1
    out["close_sma50"] = close / sma[50] - 1
    out["close_sma200"] = close / sma[200] - 1
    out["sma20_sma50"] = sma[20] / sma[50] - 1
    out["rsi14"] = _rsi(close)
    lv = np.log1p(volume)
    out["volume_z20"] = (lv - lv.rolling(20, min_periods=20).mean()) / lv.rolling(
        20, min_periods=20
    ).std()
    out["log_traded_value_20d"] = np.log1p((close * volume).rolling(20, min_periods=20).mean())
    out["high_52w"] = close / high.rolling(252, min_periods=252).max() - 1
    out["low_52w"] = close / low.rolling(252, min_periods=252).min() - 1
    out["gap_open"] = opn / prev - 1
    mkt = market.reindex(c.index)
    out["rs_nifty_20d"] = out["ret_20d"] - mkt["mkt_ret_20d"]
    out["rs_sector_20d"] = out["ret_20d"] - sector.reindex(c.index)["sector_r20"]
    for col in ("mkt_ret_5d", "mkt_ret_20d", "mkt_close_sma200", "vix_level", "vix_chg_5d"):
        out[col] = mkt[col]
    idx = pd.DatetimeIndex(pd.to_datetime(c.index))
    out["day_of_week"] = idx.day_of_week.astype(float)
    out["month"] = idx.month.astype(float)
    out["history_days"] = np.arange(1, len(c) + 1, dtype=float)
    event_set = set(events)
    hit = pd.Series([1.0 if d in event_set else 0.0 for d in c.index], index=c.index)
    out["price_event_5d"] = hit.rolling(5, min_periods=1).max()
    out["news_exchange_1d"] = _news_counts(c.index, news, "exchange", 1)
    out["news_exchange_5d"] = _news_counts(c.index, news, "exchange", 5)
    out["news_company_5d"] = _news_counts(c.index, news, "company_news", 5)
    out = out.replace([np.inf, -np.inf], np.nan)
    return out[list(FEATURE_NAMES)]


def _count_between(arr: np.ndarray, start: np.datetime64, end: np.datetime64) -> int:
    """How many sorted dates fall in (start, end]."""
    return int(np.searchsorted(arr, end, "right") - np.searchsorted(arr, start, "right"))


def build_labels(
    candles: pd.DataFrame,
    calendar: Sequence[date],
    *,
    special: Iterable[date] = (),
    events: Iterable[date] = (),
    horizons: Sequence[int] = HORIZONS,
    only: Iterable[date] | None = None,
) -> pd.DataFrame:
    """Forward labels for ONE instrument (see the module docstring). ``calendar`` = regular
    trading days (special sessions excluded); ``candles`` = the instrument's regular-session
    candles. One row per (candle day in the calendar, horizon); ``only`` limits the days."""
    cal = np.array(sorted(calendar), dtype="datetime64[D]")
    pos = {d: i for i, d in enumerate(sorted(calendar))}
    c = candles.sort_index()
    days = [d for d in c.index if d in pos]
    if only is not None:
        wanted = set(only)
        days = [d for d in days if d in wanted]
    if not days or not len(cal):
        return pd.DataFrame(columns=["trade_date", *LABEL_COLUMNS])
    has = np.zeros(len(cal), dtype=bool)
    has[[pos[d] for d in c.index if d in pos]] = True
    missing_cum = np.concatenate([[0], np.cumsum(~has)])  # missing_cum[j] = missing in cal[:j]
    special_sorted = np.array(sorted(special), dtype="datetime64[D]")
    events_sorted = np.array(sorted(events), dtype="datetime64[D]")
    last_candle = c.index.max()
    close = c["close"].astype(float)
    opn = c["open"].astype(float)
    rows = []
    for h in horizons:
        for d in days:
            p = pos[d]
            q = p + h
            reason, value = None, None
            if q >= len(cal) or cal[q].astype(date) > last_candle:
                reason = "beyond_data"
            else:
                start, end = np.datetime64(d, "D"), cal[q]
                if len(special_sorted) and _count_between(special_sorted, start, end) > 0:
                    reason = "special_session"
                elif len(events_sorted) and _count_between(events_sorted, start, end) > 0:
                    reason = "price_event"
                elif missing_cum[q + 1] - missing_cum[p + 1] > 0:
                    reason = "gap"
                else:
                    t1, th = cal[p + 1].astype(date), cal[q].astype(date)
                    value = float(close[th] / opn[t1] - 1)
            rows.append((d, h, value, None if value is None else value > 0, reason))
    out = pd.DataFrame(rows, columns=["trade_date", *LABEL_COLUMNS])
    # keep None (not NaN) for "no reason" / "no label", whatever pandas infers
    for col in ("excluded_reason", "up"):
        out[col] = out[col].astype(object).where(out[col].notna(), None)
    return out


# --- inputs for many instruments ---------------------------------------------------------


@dataclass
class Inputs:
    """Everything the builder needs, in memory: regular-session candles per instrument,
    Nifty 50 and VIX candles, sectors, events, news and the regular calendar."""

    candles: dict[int, pd.DataFrame]
    sector_of: dict[int, str | None]
    nifty: pd.DataFrame | None
    vix: pd.DataFrame | None
    events: dict[int, list[date]] = field(default_factory=dict)
    news: dict[int, list[NewsItem]] = field(default_factory=dict)
    calendar: list[date] = field(default_factory=list)
    special: list[date] = field(default_factory=list)
    symbols: dict[int, str] = field(default_factory=dict)

    def market(self) -> pd.DataFrame:
        return market_frame(self.nifty, self.vix)

    def sectors(self) -> dict[str, pd.DataFrame]:
        groups: dict[str, dict[int, pd.Series]] = {}
        for inst, frame in self.candles.items():
            sector = self.sector_of.get(inst)
            if sector:
                groups.setdefault(sector, {})[inst] = frame["close"].astype(float)
        return {name: sector_frame(members) for name, members in groups.items()}

    def sector_of_inst(self, inst: int, cache: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """The sector frame from ``cache`` (built once with :meth:`sectors`)."""
        return cache.get(self.sector_of.get(inst) or "", _EMPTY_SECTOR)

    def sector_for(self, inst: int) -> pd.DataFrame:
        sector = self.sector_of.get(inst)
        if not sector:
            return pd.DataFrame(columns=["sector_r20"], dtype=float)
        members = {
            k: f["close"].astype(float)
            for k, f in self.candles.items()
            if self.sector_of.get(k) == sector
        }
        return sector_frame(members)

    def truncated(self, t: date) -> Inputs:
        """A copy without any data after t (candles, market, events; news after
        prediction_time(t))."""
        cut = prediction_time(t)
        return replace(
            self,
            candles={k: f[f.index <= t] for k, f in self.candles.items()},
            nifty=None if self.nifty is None else self.nifty[self.nifty.index <= t],
            vix=None if self.vix is None else self.vix[self.vix.index <= t],
            events={k: [d for d in v if d <= t] for k, v in self.events.items()},
            news={k: [n for n in v if n.available_at <= cut] for k, v in self.news.items()},
            calendar=[d for d in self.calendar if d <= t],
        )


BuildFn = Callable[..., pd.DataFrame]
_EMPTY_SECTOR = pd.DataFrame(columns=["sector_r20"], dtype=float)


def features_for(
    inputs: Inputs,
    inst: int,
    *,
    market: pd.DataFrame | None = None,
    sector: pd.DataFrame | None = None,
    build: BuildFn = build_features,
) -> pd.DataFrame:
    return build(
        inputs.candles[inst],
        inputs.market() if market is None else market,
        inputs.sector_for(inst) if sector is None else sector,
        events=inputs.events.get(inst, []),
        news=inputs.news.get(inst, []),
    )


def labels_for(inputs: Inputs, inst: int, only: Iterable[date] | None = None) -> pd.DataFrame:
    return build_labels(
        inputs.candles[inst],
        inputs.calendar,
        special=inputs.special,
        events=inputs.events.get(inst, []),
        only=only,
    )


# --- the leakage test --------------------------------------------------------------------


@dataclass
class LeakageReport:
    checks: dict[str, list[str]] = field(default_factory=dict)  # check -> failures

    @property
    def passed(self) -> bool:
        return not any(self.checks.values())

    def lines(self) -> list[str]:
        out = []
        for name, failures in self.checks.items():
            out.append(f"  {name}: {'PASS' if not failures else f'FAIL ({len(failures)})'}")
            out += [f"    - {f}" for f in failures[:5]]
        out.insert(0, f"leakage check: {'PASS' if self.passed else 'FAIL'}")
        return out


def _same_row(a: pd.Series, b: pd.Series, cols: Sequence[str]) -> list[str]:
    bad = []
    for col in cols:
        x, y = a[col], b[col]
        if (pd.isna(x) and pd.isna(y)) or (
            not pd.isna(x) and not pd.isna(y) and np.isclose(x, y, rtol=1e-9, atol=1e-12)
        ):
            continue
        bad.append(col)
    return bad


def check_truncation(
    inputs: Inputs,
    samples: Sequence[tuple[int, date]],
    *,
    build: BuildFn = build_features,
    full: dict[int, pd.DataFrame] | None = None,
) -> list[str]:
    """(a) Features for t from the full history == features after deleting everything after t."""
    failures = []
    full_market, sectors = inputs.market(), inputs.sectors()
    for inst, t in samples:
        full_sector = inputs.sector_of_inst(inst, sectors)
        whole = (full or {}).get(inst)
        if whole is None:
            whole = features_for(inputs, inst, market=full_market, sector=full_sector, build=build)
        cut = inputs.truncated(t)
        part = features_for(cut, inst, build=build)
        cols = list(whole.columns)
        bad = _same_row(whole.loc[t], part.loc[t], cols)
        if bad:
            failures.append(f"instrument {inst} on {t}: {', '.join(bad)} changed")
    return failures


def check_late_news(
    inputs: Inputs,
    samples: Sequence[tuple[int, date]],
    *,
    build: BuildFn = build_features,
    full: dict[int, pd.DataFrame] | None = None,
) -> list[str]:
    """(b) News with available_at > prediction_time(t), added or changed, changes nothing for t."""
    failures = []
    market, sectors = inputs.market(), inputs.sectors()
    for inst, t in samples:
        sector = inputs.sector_of_inst(inst, sectors)
        whole = (full or {}).get(inst)
        if whole is None:
            whole = features_for(inputs, inst, market=market, sector=sector, build=build)
        before = whole.loc[t]
        late = prediction_time(t) + timedelta(seconds=1)
        extra = [NewsItem("exchange", late), NewsItem("company_news", late + timedelta(hours=3))]
        moved = [
            replace(n, available_at=n.available_at + timedelta(days=30))
            if n.available_at > prediction_time(t)
            else n
            for n in inputs.news.get(inst, [])
        ]
        changed = replace(inputs, news={**inputs.news, inst: moved + extra})
        after = features_for(changed, inst, market=market, sector=sector, build=build).loc[t]
        bad = _same_row(before, after, list(before.index))
        if bad:
            failures.append(f"instrument {inst} on {t}: late news changed {', '.join(bad)}")
    return failures


def check_names(build: BuildFn = build_features) -> list[str]:
    """(c) No feature name is or contains a label word; the builder never reads the labels."""
    failures = []
    for name in FEATURE_NAMES:
        if name in LABEL_COLUMNS or name in ("fwd_return", "up", "excluded_reason"):
            failures.append(f"feature {name} is a label column")
        if any(word in name for word in FORBIDDEN_FEATURE_WORDS):
            failures.append(f"feature {name} contains a label word")
    try:
        source = inspect.getsource(build)
    except (OSError, TypeError):
        source = ""
    for word in ("labels_daily", "build_labels", "fwd_return"):
        if word in source:
            failures.append(f"the feature builder mentions {word}")
    return failures


def check_shifted(
    inputs: Inputs,
    samples: Sequence[tuple[int, date]],
    *,
    build: BuildFn = build_features,
    full: dict[int, pd.DataFrame] | None = None,
) -> list[str]:
    """(d) Property test with shifted data: changing candles AFTER t changes no feature for t;
    changing candles UP TO t changes no label for t; changing candles after t does change an
    unexcluded label for t."""
    failures = []
    market, sectors = inputs.market(), inputs.sectors()
    for inst, t in samples:
        frame = inputs.candles[inst]
        after = frame.copy()
        mask = after.index > t
        # a factor that grows day by day, so ratios of future prices change too
        growth = np.linspace(1.1, 1.6, int(mask.sum()))[:, None]
        after.loc[mask, list(OHLCV[:4])] = after.loc[mask, list(OHLCV[:4])].to_numpy() * growth
        shifted = replace(inputs, candles={**inputs.candles, inst: after})
        sector = inputs.sector_of_inst(inst, sectors)
        whole = (full or {}).get(inst)
        if whole is None:
            whole = features_for(inputs, inst, market=market, sector=sector, build=build)
        f0 = whole.loc[t]
        f1 = features_for(shifted, inst, market=market, build=build).loc[t]
        bad = _same_row(f0, f1, list(f0.index))
        if bad:
            failures.append(
                f"instrument {inst} on {t}: future candles changed features {', '.join(bad)}"
            )
        before = frame.copy()
        before.loc[before.index <= t, list(OHLCV[:4])] *= 0.61
        l0 = labels_for(inputs, inst, [t])
        l1 = labels_for(replace(inputs, candles={**inputs.candles, inst: before}), inst, [t])
        l2 = labels_for(shifted, inst, [t])
        for h in HORIZONS:
            a = l0[(l0.trade_date == t) & (l0.horizon_days == h)]
            b = l1[(l1.trade_date == t) & (l1.horizon_days == h)]
            if len(a) and not _labels_equal(a.iloc[0], b.iloc[0]):
                failures.append(f"instrument {inst} on {t}: past candles changed the {h}-day label")
            c2 = l2[(l2.trade_date == t) & (l2.horizon_days == h)]
            if (
                len(a)
                and a.iloc[0]["excluded_reason"] is None
                and _labels_equal(a.iloc[0], c2.iloc[0])
            ):
                failures.append(
                    f"instrument {inst} on {t}: the {h}-day label ignores future candles"
                )
    return failures


def _labels_equal(a: pd.Series, b: pd.Series) -> bool:
    if a["excluded_reason"] != b["excluded_reason"]:
        return False
    x, y = a["fwd_return"], b["fwd_return"]
    return (pd.isna(x) and pd.isna(y)) or bool(np.isclose(x, y))


def run_leakage_checks(
    inputs: Inputs, samples: Sequence[tuple[int, date]], *, build: BuildFn = build_features
) -> LeakageReport:
    # the full-history features, built once per sampled instrument and shared by the checks
    market, sectors = inputs.market(), inputs.sectors()
    full = {
        inst: features_for(inputs, inst, market=market,
                           sector=inputs.sector_of_inst(inst, sectors), build=build)
        for inst in {inst for inst, _ in samples}
    }  # fmt: skip
    return LeakageReport(
        {
            "a) truncation": check_truncation(inputs, samples, build=build, full=full),
            "b) late news": check_late_news(inputs, samples, build=build, full=full),
            "c) label names": check_names(build),
            "d) shifted data": check_shifted(inputs, samples, build=build, full=full),
        }
    )


def pick_samples(inputs: Inputs, n: int, seed: int = 20261124) -> list[tuple[int, date]]:
    """n (instrument, day) pairs, spread over instruments and time (reproducible)."""
    rng = random.Random(seed)
    pairs = [(inst, d) for inst, f in inputs.candles.items() for d in f.index[30:]]
    return rng.sample(pairs, min(n, len(pairs)))


# --- database ----------------------------------------------------------------------------


def _frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["trade_date", *OHLCV])
    return df.set_index("trade_date").astype(float)


def load_inputs(
    conn: Any,
    *,
    universe: str = "nifty500",
    nifty_key: str = NIFTY_KEY,
    vix_key: str = VIX_KEY,
    settings: Settings | None = None,
    with_news: bool = True,
) -> Inputs:
    """Read candles (regular sessions only), sectors, events and news for a universe."""
    from market_intel.candles import special_sessions

    members = conn.execute(
        "SELECT i.id, i.symbol, i.sector FROM instruments i JOIN universe_members u"
        " ON u.instrument_id = i.id AND u.universe = %s WHERE i.is_active ORDER BY i.id",
        (universe,),
    ).fetchall()
    special = special_sessions(conn, universe=universe, calendar_key=nifty_key)
    special_set = set(special)
    keys = {nifty_key: None, vix_key: None}
    for key in keys:
        row = conn.execute(
            "SELECT id FROM instruments WHERE instrument_key = %s", (key,)
        ).fetchone()
        keys[key] = row[0] if row else None
    ids = [m[0] for m in members] + [v for v in keys.values() if v]
    data: dict[int, list[tuple]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, trade_date, open::float8, high::float8, low::float8,"
            " close::float8, volume::float8 FROM candles_daily WHERE instrument_id = ANY(%s)"
            " ORDER BY instrument_id, trade_date",
            (ids,),
        )
        for inst, *row in cur:
            if row[0] not in special_set:
                data.setdefault(inst, []).append(tuple(row))
    frames = {k: _frame(v) for k, v in data.items()}
    nifty = frames.pop(keys[nifty_key], None) if keys[nifty_key] else None
    vix = frames.pop(keys[vix_key], None) if keys[vix_key] else None
    events: dict[int, list[date]] = {}
    for inst, day in conn.execute("SELECT instrument_id, trade_date FROM price_events").fetchall():
        events.setdefault(inst, []).append(day)
    inputs = Inputs(
        candles={m[0]: frames[m[0]] for m in members if m[0] in frames},
        sector_of={m[0]: m[2] for m in members},
        symbols={m[0]: m[1] for m in members},
        nifty=nifty,
        vix=vix,
        events=events,
        calendar=[] if nifty is None else list(nifty.index),
        special=special,
    )
    if with_news and settings is not None:
        inputs.news = load_news(settings, inputs.symbols)
    return inputs


def load_news(settings: Settings, symbols: dict[int, str]) -> dict[int, list[NewsItem]]:
    by_symbol = {s: i for i, s in symbols.items()}
    out: dict[int, list[NewsItem]] = {}
    db = settings.require("database")
    with mongo.client(db) as client:
        coll = access.collection(client[db.mongodb_db], "news_items")
        query = {"kind": {"$in": ["exchange", "company_news"]}, "symbols.0": {"$exists": True}}
        for doc in coll.find(query, {"kind": 1, "symbols": 1, "available_at": 1}):
            when = doc["available_at"]
            when = when if when.tzinfo else when.replace(tzinfo=UTC)
            for sym in doc.get("symbols") or []:
                if sym in by_symbol:
                    out.setdefault(by_symbol[sym], []).append(NewsItem(doc["kind"], when))
    return out


def _copy_rows(conn: Any, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    n = 0
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
            n += 1
    return n


def _clean(value: Any) -> Any:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    return value


@dataclass
class BuildSummary:
    instruments: int = 0
    rows: int = 0
    seconds: float = 0.0
    null_pct: dict[str, float] = field(default_factory=dict)
    thin: int = 0  # instruments with fewer than 252 days of history

    def lines(self) -> list[str]:
        out = [
            f"features {FEATURE_VERSION}: instruments {self.instruments}, rows written {self.rows},"
            f" duration {self.seconds:.0f} s; instruments under 252 days {self.thin}"
        ]
        if self.null_pct:
            out.append("  % NULL per feature (all stored rows of the universe):")
            out += [f"    {k:<22} {v:6.2f}%" for k, v in self.null_pct.items()]
        return out


def build_and_store(
    settings: Settings,
    conn: Any,
    *,
    universe: str = "nifty500",
    start: date | None = None,
    version: str = FEATURE_VERSION,
    inputs: Inputs | None = None,
) -> BuildSummary:
    """Compute features for the universe and COPY new rows (incremental: after each
    instrument's last stored day; with ``start``: rows from that day are rebuilt)."""
    if version != FEATURE_VERSION:
        raise ConfigError(f"only feature version {FEATURE_VERSION} exists")
    t0 = time_mod.monotonic()
    inputs = inputs or load_inputs(conn, universe=universe, settings=settings)
    market = inputs.market()
    sectors = inputs.sectors()
    summary = BuildSummary(instruments=len(inputs.candles))
    last = dict(
        conn.execute(
            "SELECT instrument_id, max(trade_date) FROM features_daily"
            " WHERE feature_version = %s GROUP BY 1",
            (version,),
        ).fetchall()
    )
    cols = ["instrument_id", "trade_date", "feature_version", *FEATURE_NAMES]
    with conn.transaction():
        if start is not None:
            conn.execute(
                "DELETE FROM features_daily WHERE feature_version = %s AND trade_date >= %s"
                " AND instrument_id = ANY(%s)",
                (version, start, list(inputs.candles)),
            )
        for inst, frame in inputs.candles.items():
            sector = sectors.get(
                inputs.sector_of.get(inst) or "", pd.DataFrame(columns=["sector_r20"], dtype=float)
            )
            feats = build_features(
                frame,
                market,
                sector,
                events=inputs.events.get(inst, []),
                news=inputs.news.get(inst, []),
            )
            if len(frame) < 252:
                summary.thin += 1
            if start is not None:
                feats = feats[feats.index >= start]
            elif inst in last and last[inst] is not None:
                feats = feats[feats.index > last[inst]]
            rows = (
                (inst, d, version, *(_clean(v) for v in values))
                for d, values in zip(
                    feats.index, feats.itertuples(index=False, name=None), strict=True
                )
            )
            summary.rows += _copy_rows(conn, "features_daily", cols, rows)
    summary.null_pct = null_percentages(conn, list(inputs.candles), version)
    summary.seconds = time_mod.monotonic() - t0
    return summary


def null_percentages(conn: Any, ids: Sequence[int], version: str) -> dict[str, float]:
    parts = ", ".join(
        f"100.0 * count(*) FILTER (WHERE {n} IS NULL) / greatest(count(*), 1)"
        for n in FEATURE_NAMES
    )
    row = conn.execute(
        f"SELECT {parts} FROM features_daily"
        " WHERE feature_version = %s AND instrument_id = ANY(%s)",
        (version, list(ids)),
    ).fetchone()
    return {n: float(v or 0) for n, v in zip(FEATURE_NAMES, row, strict=True)}


@dataclass
class LabelSummary:
    rows: int = 0
    seconds: float = 0.0
    per_horizon: dict[int, dict[str, Any]] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [f"labels: rows written {self.rows}, duration {self.seconds:.0f} s"]
        for h, s in self.per_horizon.items():
            reasons = ", ".join(f"{r} {s['excluded'].get(r, 0)}" for r in EXCLUDED_REASONS)
            out.append(
                f"  {h:>3} days: usable {s['usable']}, % up {s['pct_up']:.1f}; excluded: {reasons}"
            )
        return out


def labels_and_store(
    conn: Any, *, universe: str = "nifty500", inputs: Inputs | None = None
) -> LabelSummary:
    """Recompute every label of the universe (labels change as new candles arrive) and COPY."""
    t0 = time_mod.monotonic()
    inputs = inputs or load_inputs(conn, universe=universe, with_news=False)
    summary = LabelSummary()
    cols = ["instrument_id", "trade_date", "horizon_days", "fwd_return", "up", "excluded_reason"]
    with conn.transaction():
        conn.execute(
            "DELETE FROM labels_daily WHERE instrument_id = ANY(%s)", (list(inputs.candles),)
        )
        for inst in inputs.candles:
            frame = labels_for(inputs, inst)
            rows = (
                (
                    inst,
                    r.trade_date,
                    int(r.horizon_days),
                    _clean(r.fwd_return),
                    None if r.up is None or pd.isna(r.up) else bool(r.up),
                    _clean(r.excluded_reason),
                )
                for r in frame.itertuples(index=False)
            )
            summary.rows += _copy_rows(conn, "labels_daily", cols, rows)
    ids = list(inputs.candles)
    for h, usable, up in conn.execute(
        "SELECT horizon_days, count(fwd_return), count(*) FILTER (WHERE up)"
        " FROM labels_daily WHERE instrument_id = ANY(%s) GROUP BY 1",
        (ids,),
    ).fetchall():
        summary.per_horizon[h] = {
            "usable": usable,
            "pct_up": 100.0 * up / usable if usable else 0.0,
            "excluded": {},
        }
    for h, reason, n in conn.execute(
        "SELECT horizon_days, excluded_reason, count(*) FROM labels_daily"
        " WHERE instrument_id = ANY(%s) AND excluded_reason IS NOT NULL GROUP BY 1, 2",
        (ids,),
    ).fetchall():
        summary.per_horizon.setdefault(h, {"usable": 0, "pct_up": 0.0, "excluded": {}})
        summary.per_horizon[h]["excluded"][reason] = n
    summary.per_horizon = dict(sorted(summary.per_horizon.items()))
    summary.seconds = time_mod.monotonic() - t0
    return summary


# --- entry point -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.features``."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.features")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="compute features (incremental) and store them")
    build.add_argument("--universe", default="nifty500")
    build.add_argument(
        "--from", dest="start", type=date.fromisoformat, help="rebuild from this day"
    )
    build.add_argument("--version", default=FEATURE_VERSION)
    labels = sub.add_parser("labels", help="recompute the forward labels")
    labels.add_argument("--universe", default="nifty500")
    leak = sub.add_parser("check-leakage", help="run the leakage test on the stored candles")
    leak.add_argument("--universe", default="nifty500")
    leak.add_argument("--sample", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            with postgres.connect(settings.require("database")) as conn:
                if args.command == "build":
                    params = {
                        "universe": args.universe,
                        "from": args.start and args.start.isoformat(),
                        "version": args.version,
                    }
                    with ingestion_run(
                        settings, JOB_FEATURES, source="candles_daily", params=params
                    ) as run:
                        summary = build_and_store(
                            settings,
                            conn,
                            universe=args.universe,
                            start=args.start,
                            version=args.version,
                        )
                        run.add_rows(summary.rows)
                    print("\n".join(summary.lines()))
                elif args.command == "labels":
                    with ingestion_run(
                        settings,
                        JOB_LABELS,
                        source="candles_daily",
                        params={"universe": args.universe},
                    ) as run:
                        result = labels_and_store(conn, universe=args.universe)
                        run.add_rows(result.rows)
                    print("\n".join(result.lines()))
                else:
                    inputs = load_inputs(conn, universe=args.universe, settings=settings)
                    if not inputs.candles:
                        print("Error: no candles for this universe", file=sys.stderr)
                        return 1
                    t0 = time_mod.monotonic()
                    report = run_leakage_checks(inputs, pick_samples(inputs, args.sample))
                    print("\n".join(report.lines()))
                    seconds = time_mod.monotonic() - t0
                    print(f"  samples {args.sample}, duration {seconds:.0f} s")
                    log.info("leakage_checked", passed=report.passed, samples=args.sample)
                    return 0 if report.passed else 1
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
