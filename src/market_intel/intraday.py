"""Intraday features v1, triple-barrier labels and the intraday leakage test (sprint S4i;
decisions 2026-11-25).

    uv run python -m market_intel.intraday features build [--from YYYY-MM] [--version v1]
    uv run python -m market_intel.intraday labels build [--from YYYY-MM]
    uv run python -m market_intel.intraday check-leakage [--sample N]

TIMING CONVENTION (enforced here and by the leakage test):

- Decision points every 5 minutes from 09:30 to 15:00 IST (bar starts).
- Features at decision bar t use ONLY bars up to and including t (closed), daily candles up to
  the previous trading day, and price events up to the previous trading day.
- Entry at the OPEN of bar t+1. Every position closes by the close of the 15:20 bar.

Storage: Parquet files, one per month, under ``DATA_DIR/features/intraday/<version>/year=YYYY/
month=MM/`` and ``DATA_DIR/labels/intraday/<version>/...`` (git-ignored); each build is recorded
in Postgres ``feature_builds`` with every file's SHA-256. Candles stay in Postgres.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import io
import json
import random
import re
import sys
import time as time_mod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from market_intel import universe
from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import postgres
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id

COMPONENT = "intraday"
DEFAULT_CAPITAL = 100_000.0
VERSION = "v1"
# v1 = fixed percentage barriers (S4i); v2 = ATR-scaled with short limits (S5i);
# v3 = the same ATR barriers held far longer, up to the session close (S5i-b)
LABEL_VERSIONS = ("v1", "v2", "v3")
ATR_LABEL_VERSIONS = ("v2", "v3")  # the versions whose barriers scale with the ATR
IST = ZoneInfo("Asia/Kolkata")
OPEN_IST = time(9, 15)
SESSION_BARS = 375  # 09:15 .. 15:29
DECISION_MINUTES = tuple(range(15, 346, 5))  # 09:30 .. 15:00 (minutes after 09:15)
EXIT_MINUTE = 365  # the 15:20 bar
OR15_LAST, OR30_LAST = 14, 29  # the opening ranges are complete after these bars close
THIN_DAY_BARS = 300
NIFTY_KEY, VIX_KEY = "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"
SIDES = ("long", "short")
EXCLUDED_REASONS = ("special_session", "price_event", "thin_day", "no_entry")

FEATURES_V1: tuple[str, ...] = (
    "ret_1m",
    "ret_5m",
    "ret_15m",
    "ret_30m",
    "ret_open",
    "gap_overnight",
    "vwap_dist",
    "vwap_slope_15",
    "or15_pos",
    "or15_up",
    "or15_down",
    "or30_pos",
    "or30_up",
    "or30_down",
    "vol_rel_tod",
    "cumvol_rel_tod",
    "rv_15",
    "rv_30",
    "range_so_far",
    "rs_nifty_open",
    "rs_nifty_15",
    "rs_sector_open",
    "rs_sector_15",
    "mkt_ret_open",
    "mkt_ret_15",
    "vix_level",
    "prev_ret",
    "prev_range",
    "prev_high_dist",
    "prev_low_dist",
    "prev_atr14_pct",
    "minutes_since_open",
    "day_of_week",
    "price_event_prev_day",
    "thin_so_far",
    "sig_orb_up",
    "sig_orb_down",
    "sig_vwap_up",
    "sig_vwap_down",
    "sig_volume_burst",
    "gap_bucket",
)
GROUPS: dict[str, tuple[str, ...]] = {
    "returns": ("ret_1m", "ret_5m", "ret_15m", "ret_30m", "ret_open", "gap_overnight"),
    "vwap": ("vwap_dist", "vwap_slope_15"),
    "opening range": ("or15_pos", "or15_up", "or15_down", "or30_pos", "or30_up", "or30_down"),
    "volume": ("vol_rel_tod", "cumvol_rel_tod"),
    "volatility": ("rv_15", "rv_30", "range_so_far"),
    "relative strength": ("rs_nifty_open", "rs_nifty_15", "rs_sector_open", "rs_sector_15"),
    "market": ("mkt_ret_open", "mkt_ret_15", "vix_level"),
    "previous day": ("prev_ret", "prev_range", "prev_high_dist", "prev_low_dist", "prev_atr14_pct"),
    "time and flags": ("minutes_since_open", "day_of_week", "price_event_prev_day", "thin_so_far"),
    "rule signals": (
        "sig_orb_up",
        "sig_orb_down",
        "sig_vwap_up",
        "sig_vwap_down",
        "sig_volume_burst",
        "gap_bucket",
    ),
}
LABEL_COLUMNS = ("side", "outcome", "gross_return", "bars_held", "entry_price", "excluded_reason")
ATR_BARS = 15  # the ATR window for the scaled barriers, in one-minute bars
K_GRID = (1.0, 1.5, 2.0)  # target = k x ATR, stop = k/2 x ATR
BARS_GRID = (5, 10, 15)  # v2 time limits, in bars
HOLD_TO_CLOSE = EXIT_MINUTE  # a limit this long always ends at the 15:20 bar
BARS_GRID_V3 = (30, 60, HOLD_TO_CLOSE)  # v3: half an hour, an hour, or all day
FORBIDDEN_WORDS = ("outcome", "gross_return", "label", "future", "target", "barrier")

log = get_logger("market_intel.intraday")


# --- inputs ------------------------------------------------------------------------------


@dataclass
class IntradayInputs:
    """Minute bars (UTC ts index; open, high, low, close, volume) per stock, Nifty 50, India VIX
    and the sector indices, plus daily candles, price events and special sessions."""

    bars: dict[int, pd.DataFrame]
    market: pd.DataFrame | None = None
    vix: pd.DataFrame | None = None
    sectors: dict[str, pd.DataFrame] = field(default_factory=dict)
    sector_of: dict[int, str | None] = field(default_factory=dict)
    daily: dict[int, pd.DataFrame] = field(default_factory=dict)  # date index: o, h, l, c
    daily_vix: pd.DataFrame | None = None
    events: dict[int, set[date]] = field(default_factory=dict)
    special: set[date] = field(default_factory=set)
    symbols: dict[int, str] = field(default_factory=dict)

    def truncated(self, t: pd.Timestamp, keep: int | None = None) -> IntradayInputs:
        """Everything known at the close of bar t: minute bars up to t, daily candles and
        events before t's day. ``keep``: cut (and keep) only that stock, for speed."""
        day = t.tz_convert(IST).date()

        def cut(frame: pd.DataFrame | None) -> pd.DataFrame | None:
            return None if frame is None else frame[frame.index <= t]

        def cut_daily(frame: pd.DataFrame | None) -> pd.DataFrame | None:
            return None if frame is None else frame[frame.index < day]

        return replace(
            self,
            bars={k: cut(v) for k, v in self.bars.items() if keep is None or k == keep},
            market=cut(self.market),
            vix=cut(self.vix),
            sectors={k: cut(v) for k, v in self.sectors.items()},
            daily={k: cut_daily(v) for k, v in self.daily.items() if keep is None or k == keep},
            daily_vix=cut_daily(self.daily_vix),
            events={k: {d for d in v if d < day} for k, v in self.events.items()},
        )


def _session(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the IST day and the minute after 09:15 (0..374) to minute bars."""
    local = frame.index.tz_convert(IST)
    out = frame.copy()
    out["day"] = local.date
    out["minute"] = (local.hour * 60 + local.minute) - (OPEN_IST.hour * 60 + OPEN_IST.minute)
    return out


def _index_moves(frame: pd.DataFrame | None, index: pd.Index) -> tuple[pd.Series, pd.Series]:
    """An index's log return since the day's open and over 15 minutes, aligned to ``index``."""
    if frame is None or frame.empty:
        nan = pd.Series(np.nan, index=index)
        return nan, nan
    s = _session(frame)
    logc = np.log(s["close"].astype(float))
    g = logc.groupby(s["day"])
    since_open = logc - np.log(s.groupby("day")["open"].transform("first").astype(float))
    r15 = logc - g.shift(15)
    return since_open.reindex(index), r15.reindex(index)


def _prev_daily(daily: pd.DataFrame | None, days: pd.Series) -> pd.DataFrame:
    """For each day: metrics of the last daily candle STRICTLY before it."""
    cols = ["prev_close", "prev_high", "prev_low", "prev_ret", "prev_range", "prev_atr"]
    if daily is None or daily.empty:
        return pd.DataFrame(np.nan, index=days.index, columns=cols)
    d = daily.sort_index().astype(float)
    prev_c = d["close"].shift(1)
    tr = pd.concat(
        [d["high"] - d["low"], (d["high"] - prev_c).abs(), (d["low"] - prev_c).abs()], axis=1
    ).max(axis=1)
    metrics = pd.DataFrame(
        {
            "prev_close": d["close"],
            "prev_high": d["high"],
            "prev_low": d["low"],
            "prev_ret": np.log(d["close"] / prev_c),
            "prev_range": (d["high"] - d["low"]) / d["close"],
            "prev_atr": tr.rolling(14, min_periods=14).mean(),
        }
    )
    dates = np.array(d.index, dtype="datetime64[D]")
    pos = np.searchsorted(dates, np.array(list(days), dtype="datetime64[D]"), side="left") - 1
    out = metrics.to_numpy()[np.clip(pos, 0, None)]
    out[pos < 0] = np.nan
    return pd.DataFrame(out, index=days.index, columns=cols)


def build_minute_features(inputs: IntradayInputs, inst: int) -> pd.DataFrame:
    """Features v1 for EVERY regular-session bar of one stock (look-back only; grouped by day).
    Pure: DataFrames in, DataFrame out. Use :func:`decisions` for the decision points."""
    bars = inputs.bars.get(inst)
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["trade_date", "minute", *FEATURES_V1])
    s = _session(bars.sort_index())
    day, minute = s["day"], s["minute"]
    o, h, l_, c = (s[k].astype(float) for k in ("open", "high", "low", "close"))
    v = s["volume"].astype(float)
    by_day = s.groupby("day")
    logc = np.log(c)
    out = pd.DataFrame(index=s.index)
    for k, name in ((1, "ret_1m"), (5, "ret_5m"), (15, "ret_15m"), (30, "ret_30m")):
        out[name] = logc - logc.groupby(day).shift(k)
    day_open = by_day["open"].transform("first").astype(float)
    out["ret_open"] = logc - np.log(day_open)
    prev = _prev_daily(inputs.daily.get(inst), day)
    out["gap_overnight"] = day_open / prev["prev_close"] - 1
    # VWAP of the session so far
    typ = (h + l_ + c) / 3
    cum_v = v.groupby(day).cumsum()
    vwap = (typ * v).groupby(day).cumsum() / cum_v.where(cum_v > 0)
    out["vwap_dist"] = c / vwap - 1
    out["vwap_slope_15"] = vwap / vwap.groupby(day).shift(15) - 1
    # opening ranges, defined only once complete
    for last, tag in ((OR15_LAST, "or15"), (OR30_LAST, "or30")):
        in_range = minute <= last
        hi = h.where(in_range).groupby(day).transform("max")
        lo = l_.where(in_range).groupby(day).transform("min")
        done = minute >= last
        hi, lo = hi.where(done), lo.where(done)
        width = (hi - lo).where(lambda w: w > 0)
        out[f"{tag}_pos"] = (c - lo) / width
        out[f"{tag}_up"] = (c > hi).astype(float).where(done)
        out[f"{tag}_down"] = (c < lo).astype(float).where(done)
    # volume vs the same time of day over the previous 20 days (previous days only)
    vol_tab = pd.DataFrame({"day": day, "minute": minute, "v": v, "cv": cum_v})
    for col, name, cur in (("v", "vol_rel_tod", v), ("cv", "cumvol_rel_tod", cum_v)):
        table = vol_tab.pivot_table(index="day", columns="minute", values=col, aggfunc="first")
        base = table.shift(1).rolling(20, min_periods=10).mean()
        stacked = base.stack(future_stack=True)
        lookup = stacked.reindex(pd.MultiIndex.from_arrays([day, minute])).to_numpy()
        out[name] = (cur / pd.Series(lookup, index=s.index)).where(lambda x: np.isfinite(x))
    r1 = out["ret_1m"]
    out["rv_15"] = r1.groupby(day).rolling(15, min_periods=15).std().reset_index(level=0, drop=True)
    out["rv_30"] = r1.groupby(day).rolling(30, min_periods=30).std().reset_index(level=0, drop=True)
    out["range_so_far"] = (h.groupby(day).cummax() - l_.groupby(day).cummin()) / day_open
    # market and sector
    m_open, m_15 = _index_moves(inputs.market, s.index)
    out["mkt_ret_open"], out["mkt_ret_15"] = m_open, m_15
    out["rs_nifty_open"] = out["ret_open"] - m_open
    out["rs_nifty_15"] = out["ret_15m"] - m_15
    sector = inputs.sectors.get(inputs.sector_of.get(inst) or "")
    s_open, s_15 = _index_moves(sector, s.index)
    out["rs_sector_open"] = out["ret_open"] - s_open
    out["rs_sector_15"] = out["ret_15m"] - s_15
    out["vix_level"] = _vix_level(inputs, s)
    # previous day (daily candles before t's day)
    out["prev_ret"] = prev["prev_ret"]
    out["prev_range"] = prev["prev_range"]
    out["prev_high_dist"] = c / prev["prev_high"] - 1
    out["prev_low_dist"] = c / prev["prev_low"] - 1
    out["prev_atr14_pct"] = prev["prev_atr"] / prev["prev_close"]
    # time and flags
    out["minutes_since_open"] = minute.astype(float)
    out["day_of_week"] = pd.Series(pd.to_datetime(list(day)).day_of_week, index=s.index).astype(
        float
    )
    out["price_event_prev_day"] = _event_prev_day(inputs, inst, day)
    count_so_far = c.groupby(day).cumcount() + 1
    out["thin_so_far"] = (count_so_far < 0.8 * (minute + 1)).astype(float)
    # rule signals (for S5i)
    prev_c = c.groupby(day).shift(1)
    or_hi = h.where(minute <= OR15_LAST).groupby(day).transform("max")
    or_lo = l_.where(minute <= OR15_LAST).groupby(day).transform("min")
    ready = minute > OR15_LAST
    out["sig_orb_up"] = ((c > or_hi) & (prev_c <= or_hi) & ready).astype(float)
    out["sig_orb_down"] = ((c < or_lo) & (prev_c >= or_lo) & ready).astype(float)
    prev_vwap = vwap.groupby(day).shift(1)
    out["sig_vwap_up"] = ((c > vwap) & (prev_c <= prev_vwap)).astype(float)
    out["sig_vwap_down"] = ((c < vwap) & (prev_c >= prev_vwap)).astype(float)
    out["sig_volume_burst"] = (out["vol_rel_tod"] >= 3).astype(float)
    gap = out["gap_overnight"]
    out["gap_bucket"] = np.select(
        [gap < -0.01, gap < -0.003, gap <= 0.003, gap <= 0.01, gap > 0.01],
        [-2.0, -1.0, 0.0, 1.0, 2.0],
        np.nan,
    )
    out = out.replace([np.inf, -np.inf], np.nan)
    out.insert(0, "minute", minute)
    out.insert(0, "trade_date", day)
    return out[["trade_date", "minute", *FEATURES_V1]]


def _vix_level(inputs: IntradayInputs, s: pd.DataFrame) -> pd.Series:
    """India VIX at bar t (1-minute, the last bar up to t that day), else the previous day's
    daily close."""
    if inputs.vix is not None and not inputs.vix.empty:
        vix = _session(inputs.vix.sort_index())[["day", "close"]]
        left = pd.DataFrame({"ts": s.index, "day": s["day"].to_numpy()}).sort_values("ts")
        right = (
            vix.reset_index().rename(columns={vix.index.name or "index": "ts"}).sort_values("ts")
        )
        right = right.rename(columns={right.columns[0]: "ts"})
        merged = pd.merge_asof(
            left, right[["ts", "day", "close"]], on="ts", by="day", direction="backward"
        )
        return pd.Series(
            merged.set_index("ts")["close"].astype(float).reindex(s.index).to_numpy(), index=s.index
        )
    prev = _prev_daily(inputs.daily_vix, s["day"])
    return prev["prev_close"]


def _event_prev_day(inputs: IntradayInputs, inst: int, day: pd.Series) -> pd.Series:
    """1 if a suspected split/bonus was recorded on the previous trading day (known before the
    open). The event day itself is NOT a feature: our events are detected from that day's close."""
    events = inputs.events.get(inst, set())
    daily = inputs.daily.get(inst)
    if not events or daily is None or daily.empty:
        return pd.Series(0.0, index=day.index)
    dates = np.array(sorted(daily.index), dtype="datetime64[D]")
    pos = np.searchsorted(dates, np.array(list(day), dtype="datetime64[D]"), side="left") - 1
    prev_days = [dates[p].astype(date) if p >= 0 else None for p in pos]
    return pd.Series([1.0 if p in events else 0.0 for p in prev_days], index=day.index)


def decisions(minute_features: pd.DataFrame) -> pd.DataFrame:
    """The decision points (09:30 .. 15:00 every 5 minutes) that have a bar."""
    return minute_features[minute_features["minute"].isin(DECISION_MINUTES)]


# --- labels ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Barriers:
    target_pct: float = 0.5
    stop_pct: float = 0.25
    max_bars: int = 15


DEFAULT_BARRIERS = Barriers()


def build_labels(
    inputs: IntradayInputs,
    inst: int,
    barriers: Barriers = DEFAULT_BARRIERS,
    only: Iterable[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Triple-barrier labels for every decision point of one stock, long and short.

    Entry = open of bar t+1; the path is bars t+1 .. min(t + max_bars, the 15:20 bar). Long: +1
    if the target (+target%) is hit first, -1 if the stop (-stop%) is hit first, 0 at the time
    limit (exit at that bar's close). If one bar touches both, the STOP counts first. Short is
    the mirror. Excluded (with the reason): special sessions, price-event days and the day after,
    thin days (< 300 bars), and decisions without a bar at t+1."""
    bars = inputs.bars.get(inst)
    cols = ["trade_date", "ts", "minute", *LABEL_COLUMNS]
    if bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    s = _session(bars.sort_index())
    tp, sp = barriers.target_pct / 100, barriers.stop_pct / 100
    wanted = None if only is None else set(only)
    events = inputs.events.get(inst, set())
    daily = inputs.daily.get(inst)
    day_list = sorted(set(s["day"]))
    prev_of = {}
    if daily is not None and not daily.empty:
        dd = sorted(daily.index)
        for d in day_list:
            i = bisect.bisect_left(dd, d)
            prev_of[d] = dd[i - 1] if i > 0 else None
    rows = []
    for d, g in s.groupby("day", sort=True):
        grid = np.full((SESSION_BARS, 4), np.nan)
        mins = g["minute"].to_numpy()
        ok = (mins >= 0) & (mins < SESSION_BARS)
        grid[mins[ok]] = g[["open", "high", "low", "close"]].to_numpy(dtype=float)[ok]
        ts_of = dict(zip(mins, g.index, strict=True))
        reason_day = None
        if d in inputs.special:
            reason_day = "special_session"
        elif d in events or prev_of.get(d) in events:
            reason_day = "price_event"
        elif len(g) < THIN_DAY_BARS:
            reason_day = "thin_day"
        for m in DECISION_MINUTES:
            if m not in ts_of or (wanted is not None and ts_of[m] not in wanted):
                continue
            t = ts_of[m]
            reason = reason_day
            entry = grid[m + 1, 0] if m + 1 < SESSION_BARS else np.nan
            if reason is None and not entry > 0:
                reason = "no_entry"
            for side in SIDES:
                if reason:
                    rows.append((d, t, m, side, None, None, None, None, reason))
                    continue
                last = min(m + barriers.max_bars, EXIT_MINUTE)
                hi, lo, cl = (
                    grid[m + 1 : last + 1, 1],
                    grid[m + 1 : last + 1, 2],
                    grid[m + 1 : last + 1, 3],
                )
                if side == "long":
                    stop_hit, target_hit = lo <= entry * (1 - sp), hi >= entry * (1 + tp)
                else:
                    stop_hit, target_hit = hi >= entry * (1 + sp), lo <= entry * (1 - tp)
                i_stop = int(np.argmax(stop_hit)) if stop_hit.any() else None
                i_target = int(np.argmax(target_hit)) if target_hit.any() else None
                if i_stop is not None and (i_target is None or i_stop <= i_target):
                    outcome, ret, held = -1, -sp, i_stop + 1  # stop first (also if both in one bar)
                elif i_target is not None:
                    outcome, ret, held = 1, tp, i_target + 1
                else:
                    valid = np.where(~np.isnan(cl))[0]
                    exit_close = cl[valid[-1]] if len(valid) else entry
                    held = int(valid[-1]) + 1 if len(valid) else 0
                    gross = exit_close / entry - 1
                    outcome, ret = 0, gross if side == "long" else -gross
                rows.append((d, t, m, side, outcome, float(ret), held, float(entry), None))
    out = pd.DataFrame(rows, columns=cols)
    out["excluded_reason"] = (
        out["excluded_reason"].astype(object).where(out["excluded_reason"].notna(), None)
    )
    return out


@dataclass(frozen=True)
class Variant:
    """One volatility-scaled barrier setting: target = k x ATR, stop = k/2 x ATR."""

    k: float
    max_bars: int

    @property
    def name(self) -> str:
        """A stable column name; ``cl`` means "hold to the 15:20 close". The v2 names
        (k10_b05 and friends) must never change: they are Parquet column names and are
        stored in backtest_runs."""
        bars = "cl" if self.max_bars >= HOLD_TO_CLOSE else f"{self.max_bars:02d}"
        return f"k{round(self.k * 10):02d}_b{bars}"


VARIANTS: tuple[Variant, ...] = tuple(Variant(k, bars) for k in K_GRID for bars in BARS_GRID)
VARIANTS_V3: tuple[Variant, ...] = tuple(Variant(k, b) for k in K_GRID for b in BARS_GRID_V3)
VARIANTS_BY_VERSION: dict[str, tuple[Variant, ...]] = {"v2": VARIANTS, "v3": VARIANTS_V3}
VARIANT_BY_NAME = {v.name: v for v in (*VARIANTS, *VARIANTS_V3)}


def variants_for(version: str) -> tuple[Variant, ...]:
    """The barrier grid a label version is built with."""
    try:
        return VARIANTS_BY_VERSION[version]
    except KeyError:
        raise ConfigError(
            f"{version!r} has no barrier grid; the ATR versions are {', '.join(ATR_LABEL_VERSIONS)}"
        ) from None


def variant_columns(variants: Sequence[Variant] = VARIANTS) -> list[str]:
    return [f"{p}_{v.name}" for v in variants for p in ("outcome", "gross_return", "bars_held")]


def true_range(grid: np.ndarray) -> np.ndarray:
    """True range per bar of one day's (SESSION_BARS, 4) open/high/low/close grid."""
    high, low, close = grid[:, 1], grid[:, 2], grid[:, 3]
    prev_close = np.concatenate([[np.nan], close[:-1]])
    # fmax ignores a NaN when the other value is real, and keeps NaN only where all three are
    return np.fmax(np.fmax(high - low, np.abs(high - prev_close)), np.abs(low - prev_close))


def atr_series(grid: np.ndarray, bars: int = ATR_BARS) -> np.ndarray:
    """Average true range over the last ``bars`` bars ENDING at each minute (look-back only;
    NaN until the window is full)."""
    tr = true_range(grid)
    out = np.full(len(tr), np.nan)
    frame = pd.Series(tr).rolling(bars, min_periods=bars).mean()
    out[: len(tr)] = frame.to_numpy()
    return out


def build_labels_volatility(
    inputs: IntradayInputs,
    inst: int,
    variants: Sequence[Variant] = VARIANTS,
    only: Iterable[pd.Timestamp] | None = None,
    atr_bars: int = ATR_BARS,
) -> pd.DataFrame:
    """Labels v2: the same triple barrier, but scaled by each stock's own recent volatility.

    At decision bar t the ATR of the last ``atr_bars`` one-minute bars (bars up to and
    including t) sets the distances: target = k x ATR, stop = k/2 x ATR, so the reward-to-risk
    ratio is always 2:1 while the width follows the stock and the time of day. Entry is still
    open(t+1), the stop still counts first when one bar touches both, and everything is still
    flat by the 15:20 bar. Every variant is a column, so one row per (decision point, side).
    An extra exclusion reason, ``no_atr``, covers the first bars of a day where the ATR window
    is not yet full."""
    bars = inputs.bars.get(inst)
    base = ["trade_date", "ts", "minute", "side", "entry_price", "atr", "excluded_reason"]
    cols = base + variant_columns(variants)
    if bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    s = _session(bars.sort_index())
    wanted = None if only is None else set(only)
    events = inputs.events.get(inst, set())
    daily = inputs.daily.get(inst)
    prev_of = {}
    if daily is not None and not daily.empty:
        dd = sorted(daily.index)
        for d in sorted(set(s["day"])):
            i = bisect.bisect_left(dd, d)
            prev_of[d] = dd[i - 1] if i > 0 else None
    longest = max(v.max_bars for v in variants)
    rows = []
    for d, g in s.groupby("day", sort=True):
        grid = np.full((SESSION_BARS, 4), np.nan)
        mins = g["minute"].to_numpy()
        ok = (mins >= 0) & (mins < SESSION_BARS)
        grid[mins[ok]] = g[["open", "high", "low", "close"]].to_numpy(dtype=float)[ok]
        ts_of = dict(zip(mins, g.index, strict=True))
        atr = atr_series(grid, atr_bars)
        reason_day = None
        if d in inputs.special:
            reason_day = "special_session"
        elif d in events or prev_of.get(d) in events:
            reason_day = "price_event"
        elif len(g) < THIN_DAY_BARS:
            reason_day = "thin_day"
        for m in DECISION_MINUTES:
            if m not in ts_of or (wanted is not None and ts_of[m] not in wanted):
                continue
            t = ts_of[m]
            reason = reason_day
            entry = grid[m + 1, 0] if m + 1 < SESSION_BARS else np.nan
            if reason is None and not entry > 0:
                reason = "no_entry"
            width = atr[m]
            if reason is None and not width > 0:
                reason = "no_atr"
            last = min(m + longest, EXIT_MINUTE)
            hi_all, lo_all, cl_all = (
                grid[m + 1 : last + 1, 1],
                grid[m + 1 : last + 1, 2],
                grid[m + 1 : last + 1, 3],
            )
            for side in SIDES:
                if reason:
                    rows.append((d, t, m, side, None, None, reason) + (None,) * (len(cols) - 7))
                    continue
                values: list[object] = []
                for v in variants:
                    n = min(v.max_bars, EXIT_MINUTE - m)
                    hi, lo, cl = hi_all[:n], lo_all[:n], cl_all[:n]
                    target_dist, stop_dist = v.k * width, v.k / 2 * width
                    if side == "long":
                        stop_hit = lo <= entry - stop_dist
                        target_hit = hi >= entry + target_dist
                    else:
                        stop_hit = hi >= entry + stop_dist
                        target_hit = lo <= entry - target_dist
                    i_stop = int(np.argmax(stop_hit)) if stop_hit.any() else None
                    i_target = int(np.argmax(target_hit)) if target_hit.any() else None
                    if i_stop is not None and (i_target is None or i_stop <= i_target):
                        values += [-1, -stop_dist / entry, i_stop + 1]  # stop first
                    elif i_target is not None:
                        values += [1, target_dist / entry, i_target + 1]
                    else:
                        valid = np.where(~np.isnan(cl))[0]
                        exit_close = cl[valid[-1]] if len(valid) else entry
                        held = int(valid[-1]) + 1 if len(valid) else 0
                        gross = exit_close / entry - 1
                        values += [0, gross if side == "long" else -gross, held]
                rows.append((d, t, m, side, float(entry), float(width), None, *values))
    out = pd.DataFrame(rows, columns=cols)
    out["excluded_reason"] = (
        out["excluded_reason"].astype(object).where(out["excluded_reason"].notna(), None)
    )
    return out


# --- the leakage test --------------------------------------------------------------------

FeatureFn = Callable[[IntradayInputs, int], pd.DataFrame]
LabelFn = Callable[..., pd.DataFrame]


class LeakageError(RuntimeError):
    """The leakage test failed: nothing is written."""


@dataclass
class LeakageReport:
    checks: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(self.checks.values())

    def lines(self) -> list[str]:
        out = [f"intraday leakage check: {'PASS' if self.passed else 'FAIL'}"]
        for name, failures in self.checks.items():
            out.append(f"  {name}: {'PASS' if not failures else f'FAIL ({len(failures)})'}")
            out += [f"    - {f}" for f in failures[:5]]
        return out


def _row(frame: pd.DataFrame, t: pd.Timestamp) -> pd.Series | None:
    return frame.loc[t] if t in frame.index else None


def _diff(a: pd.Series, b: pd.Series, cols: Sequence[str]) -> list[str]:
    bad = []
    for col in cols:
        x, y = a[col], b[col]
        if (pd.isna(x) and pd.isna(y)) or (
            not pd.isna(x)
            and not pd.isna(y)
            and np.isclose(float(x), float(y), rtol=1e-9, atol=1e-12)
        ):
            continue
        bad.append(col)
    return bad


def check_truncation(
    inputs: IntradayInputs,
    samples: Sequence[tuple[int, pd.Timestamp]],
    build: FeatureFn = build_minute_features,
) -> list[str]:
    """(a) Features at t from all the data == features after deleting every bar after t."""
    failures, cache = [], {}
    for inst, t in samples:
        if inst not in cache:
            cache[inst] = build(inputs, inst)
        full = _row(cache[inst], t)
        part = _row(build(inputs.truncated(t, keep=inst), inst), t)
        if full is None or part is None:
            continue
        cols = [c for c in cache[inst].columns if c not in ("trade_date", "minute")]
        bad = _diff(full, part, cols)
        if bad:
            failures.append(f"instrument {inst} at {t}: {', '.join(bad)} changed")
    return failures


def check_time_of_day_baseline(
    inputs: IntradayInputs,
    samples: Sequence[tuple[int, pd.Timestamp]],
    build: FeatureFn = build_minute_features,
) -> list[str]:
    """(b) The time-of-day volume baseline uses only previous days: multiplying bar t's volume
    by 10 must multiply vol_rel_tod at t by exactly 10."""
    failures = []
    for inst, t in samples:
        before = _row(build(inputs, inst), t)
        bars = inputs.bars[inst].copy()
        bars.loc[t, "volume"] = bars.loc[t, "volume"] * 10 + 10
        after = _row(build(replace(inputs, bars={**inputs.bars, inst: bars}), inst), t)
        if before is None or after is None or pd.isna(before["vol_rel_tod"]):
            continue
        expected = (
            before["vol_rel_tod"] * (bars.loc[t, "volume"] / inputs.bars[inst].loc[t, "volume"])
            if inputs.bars[inst].loc[t, "volume"]
            else None
        )
        if expected is not None and not np.isclose(after["vol_rel_tod"], expected, rtol=1e-9):
            failures.append(f"instrument {inst} at {t}: the baseline moved with today's volume")
    return failures


def check_opening_range(
    inputs: IntradayInputs, instruments: Iterable[int], build: FeatureFn = build_minute_features
) -> list[str]:
    """(c) Opening-range features are undefined (NaN) before the range is complete."""
    failures = []
    for inst in instruments:
        f = build(inputs, inst)
        for tag, last in (("or15", OR15_LAST), ("or30", OR30_LAST)):
            early = f[f["minute"] < last][[f"{tag}_pos", f"{tag}_up", f"{tag}_down"]]
            if early.notna().any().any():
                failures.append(f"instrument {inst}: {tag} defined before minute {last}")
    return failures


def check_labels(
    inputs: IntradayInputs,
    samples: Sequence[tuple[int, pd.Timestamp]],
    barriers: Barriers = DEFAULT_BARRIERS,
    labels: LabelFn = build_labels,
) -> list[str]:
    """(d) Labels use only bars after t and enter at the open of bar t+1: the entry price is
    that open, and changing any bar up to t changes no label."""
    failures = []
    for inst, t in samples:
        bars = inputs.bars[inst]
        base = labels(inputs, inst, barriers, only=[t])
        if base.empty:
            continue
        later = bars[bars.index > t]
        for r in base.itertuples(index=False):
            if r.excluded_reason is None and (
                later.empty or not np.isclose(r.entry_price, later["open"].iloc[0])
            ):
                failures.append(f"instrument {inst} at {t}: entry is not the next bar's open")
        shaken = bars.copy()
        mask = shaken.index <= t
        shaken.loc[mask, ["open", "high", "low", "close"]] *= np.linspace(
            0.7, 1.3, int(mask.sum())
        )[:, None]
        moved = labels(
            replace(inputs, bars={**inputs.bars, inst: shaken}), inst, barriers, only=[t]
        )
        for a, b in zip(base.itertuples(index=False), moved.itertuples(index=False), strict=False):
            same = a.excluded_reason == b.excluded_reason and (
                (a.gross_return is None and b.gross_return is None)
                or (
                    a.gross_return is not None
                    and b.gross_return is not None
                    and np.isclose(a.gross_return, b.gross_return)
                )
            )
            if not same:
                failures.append(
                    f"instrument {inst} at {t} ({a.side}): bars up to t changed the label"
                )
    return failures


def check_labels_volatility(
    inputs: IntradayInputs,
    samples: Sequence[tuple[int, pd.Timestamp]],
    labels: LabelFn = None,  # type: ignore[assignment]
    variants: Sequence[Variant] = VARIANTS,
) -> list[str]:
    """(d2) Labels v2: the barrier width may use bars up to t (that is the point of an ATR
    barrier), so the v1 check does not apply. What must hold is narrower:

    - the entry is the open of bar t+1;
    - the ATR at t does NOT move when later bars change (it is a look-back window);
    - no label changes when bars after the longest path has ended are changed.
    """
    labels = labels or build_labels_volatility
    longest = max(v.max_bars for v in variants)
    failures = []
    for inst, t in samples:
        bars = inputs.bars[inst]
        base = labels(inputs, inst, variants, only=[t])
        if base.empty or base["excluded_reason"].notna().all():
            continue
        later = bars[bars.index > t]
        first = base.iloc[0]
        if later.empty or not np.isclose(float(first["entry_price"]), float(later["open"].iloc[0])):
            failures.append(f"instrument {inst} at {t}: entry is not the next bar's open")
        # the ATR is a look-back window, so NO later bar may move it. This is checked on its
        # own, because a hold-to-close variant has no bars after its exit at all and the
        # label-stability check below would then be vacuous.
        after_t = bars.index > t
        if after_t.any():
            shaken = bars.copy()
            shaken.loc[after_t, ["open", "high", "low", "close"]] *= 1.5
            moved = labels(
                replace(inputs, bars={**inputs.bars, inst: shaken}), inst, variants, only=[t]
            )
            if moved.empty:
                failures.append(f"instrument {inst} at {t}: the label disappeared")
                continue
            if not np.isclose(float(first["atr"]), float(moved.iloc[0]["atr"]), rtol=1e-9):
                failures.append(f"instrument {inst} at {t}: the ATR moved with later bars")
        # everything from the exit bar onwards may be rewritten freely
        local = bars.index.tz_convert(IST)
        minute = int(local[bars.index.get_loc(t)].hour * 60 + local[bars.index.get_loc(t)].minute)
        minute -= OPEN_IST.hour * 60 + OPEN_IST.minute
        cut = min(minute + longest, EXIT_MINUTE)
        beyond = bars.index > t + pd.Timedelta(minutes=cut - minute)
        if not beyond.any():
            continue  # a hold-to-close variant: the exit IS the session close
        shaken = bars.copy()
        shaken.loc[beyond, ["open", "high", "low", "close"]] *= 1.5
        moved = labels(
            replace(inputs, bars={**inputs.bars, inst: shaken}), inst, variants, only=[t]
        )
        if moved.empty:
            failures.append(f"instrument {inst} at {t}: the label disappeared")
            continue
        after = moved.iloc[0]
        for v in variants:
            a, b = first[f"outcome_{v.name}"], after[f"outcome_{v.name}"]
            if a != b:
                failures.append(
                    f"instrument {inst} at {t}: {v.name} changed with bars after the exit"
                )
                break
    return failures


def check_names() -> list[str]:
    return [
        f"feature {n} looks like a label"
        for n in FEATURES_V1
        if any(w in n for w in FORBIDDEN_WORDS)
    ]


def run_leakage_checks(
    inputs: IntradayInputs,
    samples: Sequence[tuple[int, pd.Timestamp]],
    build: FeatureFn = build_minute_features,
    labels: LabelFn = build_labels,
    barriers: Barriers = DEFAULT_BARRIERS,
    label_version: str = "v2",
) -> LeakageReport:
    insts = sorted({i for i, _ in samples})
    return LeakageReport(
        {
            "a) truncation": check_truncation(inputs, samples, build),
            "b) time-of-day baseline": check_time_of_day_baseline(inputs, samples[:50], build),
            "c) opening range": check_opening_range(inputs, insts[:10], build),
            "d) labels after t, entry at t+1 open": check_labels(inputs, samples, barriers, labels),
            f"d2) {label_version} labels: ATR looks back, path ends at the exit": (
                check_labels_volatility(inputs, samples, variants=variants_for(label_version))
            ),
            "e) names": check_names(),
        }
    )


def pick_samples(
    inputs: IntradayInputs, n: int, seed: int = 20261125
) -> list[tuple[int, pd.Timestamp]]:
    rng = random.Random(seed)
    pairs = []
    for inst, frame in inputs.bars.items():
        s = _session(frame)
        # decision points after a 20-day warm-up for the volume baseline
        days = sorted(set(s["day"]))
        keep = set(days[20:]) if len(days) > 20 else set(days[-1:])
        pairs += [
            (inst, t)
            for t, d, m in zip(s.index, s["day"], s["minute"], strict=True)
            if d in keep and m in DECISION_MINUTES
        ]
    return rng.sample(pairs, min(n, len(pairs)))


# --- database and Parquet ----------------------------------------------------------------


def _copy_csv(conn: Any, sql: str) -> pd.DataFrame:
    buf = io.BytesIO()
    with conn.cursor() as cur, cur.copy(f"COPY ({sql}) TO STDOUT WITH CSV HEADER") as copy:
        for chunk in copy:
            buf.write(chunk)
    buf.seek(0)
    return pd.read_csv(buf)


def load_inputs(
    conn: Any,
    first: date,
    last: date,
    *,
    universe: str = "nifty50",
    lookback_days: int = 45,
    nifty_key: str = NIFTY_KEY,
    vix_key: str = VIX_KEY,
) -> IntradayInputs:
    """Minute bars from ``first - lookback_days`` to ``last`` (end of day), daily candles,
    events and special sessions for the universe and its indices."""
    from market_intel.candles import special_sessions
    from market_intel.intraday_candles import SECTOR_INDEX

    if not re.fullmatch(r"[a-z0-9_]+", universe):
        raise ConfigError("bad universe name")
    members = conn.execute(
        "SELECT i.id, i.symbol, i.sector FROM instruments i JOIN universe_members u"
        " ON u.instrument_id = i.id AND u.universe = %s WHERE i.is_active ORDER BY i.id",
        (universe,),
    ).fetchall()
    keys = [
        nifty_key,
        vix_key,
        *sorted({SECTOR_INDEX[m[2]] for m in members if m[2] in SECTOR_INDEX}),
    ]
    key_ids = dict(
        conn.execute(
            "SELECT instrument_key, id FROM instruments WHERE instrument_key = ANY(%s)", (keys,)
        ).fetchall()
    )
    ids = [m[0] for m in members] + list(key_ids.values())
    id_list = ",".join(str(int(i)) for i in ids) or "0"
    start = datetime.combine(first - timedelta(days=lookback_days), time(0), IST).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0), IST).astimezone(UTC)
    minute = _copy_csv(
        conn,
        "SELECT instrument_id, ts, open::float8 AS open, high::float8 AS high, low::float8 AS low,"
        " close::float8 AS close, volume::float8 AS volume FROM candles_intraday"
        f" WHERE interval = '1m' AND instrument_id IN ({id_list})"
        f" AND ts >= '{start.isoformat()}' AND ts < '{end.isoformat()}'",
    )
    minute["ts"] = pd.to_datetime(minute["ts"], utc=True)
    frames = {
        int(k): g.drop(columns="instrument_id").set_index("ts").sort_index()
        for k, g in minute.groupby("instrument_id")
    }
    daily = _copy_csv(
        conn,
        "SELECT instrument_id, trade_date, open::float8 AS open, high::float8 AS high,"
        " low::float8 AS low, close::float8 AS close FROM candles_daily"
        f" WHERE instrument_id IN ({id_list})"
        f" AND trade_date >= '{first - timedelta(days=lookback_days + 60)}'"
        f" AND trade_date <= '{last}'",
    )
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.date
    daily_frames = {
        int(k): g.drop(columns="instrument_id").set_index("trade_date").sort_index()
        for k, g in daily.groupby("instrument_id")
    }
    events: dict[int, set[date]] = {}
    for inst, d in conn.execute("SELECT instrument_id, trade_date FROM price_events").fetchall():
        events.setdefault(inst, set()).add(d)
    sector_frames = {}
    sector_keys = {m[2]: SECTOR_INDEX.get(m[2]) for m in members}
    for sector, key in sector_keys.items():
        if key and key in key_ids and key_ids[key] in frames:
            sector_frames[sector] = frames[key_ids[key]]
    nifty_id, vix_id = key_ids.get(nifty_key), key_ids.get(vix_key)
    return IntradayInputs(
        bars={m[0]: frames[m[0]] for m in members if m[0] in frames},
        market=frames.get(nifty_id) if nifty_id else None,
        vix=frames.get(vix_id) if vix_id else None,
        sectors=sector_frames,
        sector_of={m[0]: m[2] for m in members},
        daily={m[0]: daily_frames[m[0]] for m in members if m[0] in daily_frames},
        daily_vix=daily_frames.get(vix_id) if vix_id else None,
        events=events,
        special=set(special_sessions(conn)),
        symbols={m[0]: m[1] for m in members},
    )


def month_dir(
    settings: Settings, kind: str, version: str, month: date, universe: str = "nifty50"
) -> Path:
    return built_root(settings, kind, version, universe) / f"year={month:%Y}" / f"month={month:%m}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def month_starts(first: date, last: date) -> list[date]:
    out, m = [], date(first.year, first.month, 1)
    while m <= last:
        out.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def month_end(m: date) -> date:
    return date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1)


@dataclass
class BuildSummary:
    kind: str
    version: str
    months: int = 0
    rows: int = 0
    files: int = 0
    bytes_on_disk: int = 0
    seconds: float = 0.0
    null_pct: dict[str, float] = field(default_factory=dict)
    label_mix: dict[str, dict[str, float]] = field(default_factory=dict)
    variant_mix: dict[str, dict[str, float]] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"{self.kind} {self.version}: months {self.months}, rows {self.rows:,},"
            f" files {self.files},"
            f" {self.bytes_on_disk / 1e6:,.1f} MB on disk, duration {self.seconds:.0f} s"
        ]
        if self.null_pct:
            out.append(
                "  % NULL by feature group (decision rows): "
                + ", ".join(f"{k} {v:.1f}%" for k, v in self.null_pct.items())
            )
        for side, mix in self.label_mix.items():
            out.append(
                f"  {side}: target first {mix['target']:.1f}%, stop first {mix['stop']:.1f}%,"
                f" time limit {mix['time']:.1f}%,"
                f" average gross return {mix['avg_return'] * 100:.3f}%"
                f" (usable rows {int(mix['rows']):,})"
            )
        for name, mix in sorted(self.variant_mix.items()):
            out.append(
                f"  {name}: target first {mix['target']:.1f}%, stop first {mix['stop']:.1f}%,"
                f" time limit {mix['time']:.1f}%,"
                f" average gross return {mix['avg_return'] * 100:+.4f}%"
            )
        if self.excluded:
            out.append(
                "  excluded: " + ", ".join(f"{k} {v:,}" for k, v in sorted(self.excluded.items()))
            )
        return out


def built_root(settings: Settings, kind: str, version: str, universe: str = "nifty50") -> Path:
    """Where a built dataset lives. The universe is part of the path, so building one universe
    can never overwrite another's months (S5i-c)."""
    tree = "features" if kind == "intraday_features" else "labels"
    return settings.data_dir / tree / "intraday" / universe / version


def load_built(
    settings: Settings,
    kind: str,
    version: str = VERSION,
    *,
    universe: str = "nifty50",
    start: date | None = None,
    end: date | None = None,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read a built Parquet dataset back, optionally only the months that overlap start..end."""
    root = built_root(settings, kind, version, universe)
    if not root.exists():
        raise ConfigError(f"nothing built yet at {root.name}: run the build first")
    frames = []
    for path in sorted(root.rglob("*.parquet")):
        year = int(path.parent.parent.name.split("=")[1])
        month = int(path.parent.name.split("=")[1])
        first, last = date(year, month, 1), month_end(date(year, month, 1))
        if (start and last < start) or (end and first > end):
            continue
        frames.append(pd.read_parquet(path, columns=list(columns) if columns else None))
    if not frames:
        raise ConfigError(f"no months of {kind} {version} in the requested range")
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    if start:
        out = out[out["trade_date"] >= start]
    if end:
        out = out[out["trade_date"] <= end]
    return out.reset_index(drop=True)


def last_build_to(conn: Any, kind: str, version: str) -> date | None:
    row = conn.execute(
        "SELECT max(to_date) FROM feature_builds WHERE kind = %s AND version = %s", (kind, version)
    ).fetchone()
    return row[0] if row else None


def data_range(conn: Any, universe: str = "nifty50") -> tuple[date | None, date | None]:
    row = conn.execute(
        "SELECT min(c.ts), max(c.ts) FROM candles_intraday c JOIN universe_members u"
        " ON u.instrument_id = c.instrument_id AND u.universe = %s WHERE c.interval = '1m'",
        (universe,),
    ).fetchone()
    if not row or row[0] is None:
        return None, None
    return row[0].astimezone(IST).date(), row[1].astimezone(IST).date()


def build(
    settings: Settings,
    conn: Any,
    kind: str,
    *,
    start: date | None = None,
    version: str = VERSION,
    universe: str = "nifty50",
    barriers: Barriers | None = None,
    progress: Callable[[str], None] = lambda m: None,
    gate_samples: int = 10,
) -> BuildSummary:
    """Build one month at a time (incremental: from the month of the last build), write Parquet,
    record the build. ``kind``: intraday_features or intraday_labels."""
    if kind == "intraday_features" and version != VERSION:
        raise ConfigError(f"only intraday feature version {VERSION} exists")
    if kind == "intraday_labels" and version not in LABEL_VERSIONS:
        raise ConfigError(f"label versions are {', '.join(LABEL_VERSIONS)}")
    variants = variants_for(version) if version in ATR_LABEL_VERSIONS else ()
    t0 = time_mod.monotonic()
    barriers = barriers or Barriers(
        settings.intraday_target_pct, settings.intraday_stop_pct, settings.intraday_max_bars
    )
    first_data, last_data = data_range(conn, universe)
    if first_data is None:
        raise ConfigError(
            f"no 1-minute candles for universe {universe!r}: run `candles intraday` first"
        )
    previous = last_build_to(conn, kind, version)
    begin = start or (date(previous.year, previous.month, 1) if previous else first_data)
    begin = max(begin, first_data)
    summary = BuildSummary(kind, version)
    hashes: dict[str, str] = {}
    totals = {"null": None, "rows": 0}
    null_counts = pd.Series(0.0, index=list(FEATURES_V1))
    mix = {s: {"target": 0, "stop": 0, "time": 0, "ret": 0.0, "rows": 0} for s in SIDES}
    variant_mix: dict[tuple[str, str], dict[str, float]] = {}
    for month in month_starts(begin, last_data):
        progress(f"{kind} {month:%Y-%m}")
        inputs = load_inputs(conn, month, min(month_end(month), last_data), universe=universe)
        if gate_samples and summary.months == 0:  # a leak fails the build before any write
            gate = run_leakage_checks(
                inputs,
                pick_samples(inputs, gate_samples),
                label_version=version if version in ATR_LABEL_VERSIONS else "v2",
            )
            if not gate.passed:
                raise LeakageError("\n".join(gate.lines()))
        parts = []
        for inst in inputs.bars:
            if kind == "intraday_features":
                f = decisions(build_minute_features(inputs, inst))
                f = f[(f["trade_date"] >= month) & (f["trade_date"] <= month_end(month))]
            elif version in ATR_LABEL_VERSIONS:
                f = build_labels_volatility(inputs, inst, variants)
                f = f[(f["trade_date"] >= month) & (f["trade_date"] <= month_end(month))]
            else:
                f = build_labels(inputs, inst, barriers)
                f = f[(f["trade_date"] >= month) & (f["trade_date"] <= month_end(month))]
            if len(f):
                f = f.reset_index(names="ts") if kind == "intraday_features" else f
                f.insert(0, "instrument_id", inst)
                parts.append(f)
        folder = month_dir(settings, kind, version, month, universe)
        for old in folder.glob("*.parquet") if folder.exists() else []:
            old.unlink()
        if not parts:
            continue
        frame = pd.concat(parts, ignore_index=True)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame["minute"] = frame["minute"].astype("int16")
        frame.insert(1, "version", version)
        if kind == "intraday_labels" and version in ATR_LABEL_VERSIONS:
            for v in variants:
                frame[f"outcome_{v.name}"] = frame[f"outcome_{v.name}"].astype("Int8")
                frame[f"bars_held_{v.name}"] = frame[f"bars_held_{v.name}"].astype("Int16")
                frame[f"gross_return_{v.name}"] = frame[f"gross_return_{v.name}"].astype("float64")
            usable = frame[frame["excluded_reason"].isna()]
            for side in SIDES:
                part = usable[usable["side"] == side]
                for v in variants:
                    col = f"outcome_{v.name}"
                    cell = variant_mix.setdefault((side, v.name), {})
                    cell["target"] = cell.get("target", 0) + int((part[col] == 1).sum())
                    cell["stop"] = cell.get("stop", 0) + int((part[col] == -1).sum())
                    cell["time"] = cell.get("time", 0) + int((part[col] == 0).sum())
                    cell["ret"] = cell.get("ret", 0.0) + float(part[f"gross_return_{v.name}"].sum())
                    cell["rows"] = cell.get("rows", 0) + len(part)
            for reason, n in frame["excluded_reason"].value_counts().items():
                summary.excluded[reason] = summary.excluded.get(reason, 0) + int(n) // 2
        elif kind == "intraday_labels":
            frame["outcome"] = frame["outcome"].astype("Int8")
            frame["bars_held"] = frame["bars_held"].astype("Int16")
            frame["gross_return"] = frame["gross_return"].astype("float64")
            frame["entry_price"] = frame["entry_price"].astype("float64")
            usable = frame[frame["excluded_reason"].isna()]
            for side in SIDES:
                part = usable[usable["side"] == side]
                mix[side]["target"] += int((part["outcome"] == 1).sum())
                mix[side]["stop"] += int((part["outcome"] == -1).sum())
                mix[side]["time"] += int((part["outcome"] == 0).sum())
                mix[side]["ret"] += float(part["gross_return"].sum())
                mix[side]["rows"] += len(part)
            for reason, n in frame["excluded_reason"].value_counts().items():
                summary.excluded[reason] = summary.excluded.get(reason, 0) + int(n) // 2
        else:
            null_counts += frame[list(FEATURES_V1)].isna().sum()
            totals["rows"] += len(frame)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "part-0.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        rel = path.relative_to(settings.data_dir).as_posix()
        hashes[rel] = sha256(path)
        summary.months += 1
        summary.rows += len(frame)
        summary.files += 1
        summary.bytes_on_disk += path.stat().st_size
    if summary.files:
        conn.execute(
            "INSERT INTO feature_builds (kind, version, from_date, to_date, rows, files, sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (kind, version, begin, last_data, summary.rows, summary.files, json.dumps(hashes)),
        )
    if kind == "intraday_features" and totals["rows"]:
        summary.null_pct = {
            g: float(null_counts[list(cols)].sum() / (totals["rows"] * len(cols)) * 100)
            for g, cols in GROUPS.items()
        }
    if kind == "intraday_labels" and version in ATR_LABEL_VERSIONS:
        for (side, name), m in variant_mix.items():
            n = m["rows"] or 1
            summary.variant_mix[f"{side} {name}"] = {
                "target": 100 * m["target"] / n,
                "stop": 100 * m["stop"] / n,
                "time": 100 * m["time"] / n,
                "avg_return": m["ret"] / n,
                "rows": m["rows"],
            }
    elif kind == "intraday_labels":
        for side, m in mix.items():
            n = m["rows"] or 1
            summary.label_mix[side] = {
                "target": 100 * m["target"] / n,
                "stop": 100 * m["stop"] / n,
                "time": 100 * m["time"] / n,
                "avg_return": m["ret"] / n,
                "rows": m["rows"],
            }
    summary.bytes_on_disk = sum(
        path.stat().st_size
        for path in built_root(settings, kind, version, universe).rglob("*.parquet")
    )
    summary.seconds = time_mod.monotonic() - t0
    return summary


def check_real(
    conn: Any,
    n: int,
    *,
    universe: str = "nifty50",
    months_to_check: int = 4,
    seed: int = 20261125,
    label_version: str = "v2",
) -> tuple[LeakageReport, int]:
    """The leakage test on stored candles: a few random months, n samples in all."""
    first, last = data_range(conn, universe)
    if first is None:
        raise ConfigError("no 1-minute candles yet")
    rng = random.Random(seed)
    candidates = month_starts(first, last)[1:] or month_starts(first, last)
    chosen = sorted(rng.sample(candidates, min(months_to_check, len(candidates))))
    report = LeakageReport({})
    total = 0
    for i, month in enumerate(chosen):
        inputs = load_inputs(conn, month, min(month_end(month), last), universe=universe)
        share = n // len(chosen) + (1 if i < n % len(chosen) else 0)
        samples = pick_samples(inputs, share, seed + i)
        total += len(samples)
        part = run_leakage_checks(inputs, samples, label_version=label_version)
        for name, failures in part.checks.items():
            report.checks.setdefault(name, []).extend(failures)
    return report, total


# --- entry point -------------------------------------------------------------------------


def _month(text: str) -> date:
    """A --from / --to value: YYYY-MM (the first of that month) or a full date."""
    return date.fromisoformat(text if len(text) > 7 else text + "-01")


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.intraday``."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.intraday")
    sub = parser.add_subparsers(dest="group", required=True)
    feats = sub.add_parser("features", help="intraday features")
    feats.add_argument("action", choices=["build"])
    feats.add_argument("--from", dest="start", type=_month)
    feats.add_argument("--version", default=VERSION)
    feats.add_argument("--universe", default="nifty50")
    labels = sub.add_parser("labels", help="triple-barrier labels")
    labels.add_argument("action", choices=["build"])
    labels.add_argument("--version", default=VERSION, choices=list(LABEL_VERSIONS))
    labels.add_argument("--from", dest="start", type=_month)
    labels.add_argument("--universe", default="nifty50")
    leak = sub.add_parser("check-leakage", help="the intraday leakage test on stored candles")
    leak.add_argument("--sample", type=int, default=200)
    leak.add_argument("--universe", default="nifty50")
    leak.add_argument(
        "--labels", dest="label_version", default="v2", choices=list(ATR_LABEL_VERSIONS)
    )

    def dates(target: Any) -> None:
        target.add_argument("--from", dest="start", type=_month)
        target.add_argument("--to", dest="end", type=_month)

    back = sub.add_parser("backtest", help="rule strategies against the baselines, after costs")
    dates(back)
    back.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    back.add_argument("--universe", default="nifty50")
    back.add_argument("--labels", dest="label_version", default="v2", choices=list(LABEL_VERSIONS))
    back.add_argument("--variant", default="k15_b10", choices=sorted(VARIANT_BY_NAME))
    back.add_argument("--slippage", type=float, default=None, help="bps per side for the headline")
    back.add_argument("--no-save", action="store_true", help="do not write backtest_runs")

    train = sub.add_parser("train", help="the LightGBM meta-model, walk-forward then holdout")
    dates(train)
    train.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    train.add_argument("--universe", default="nifty50")
    train.add_argument("--labels", dest="label_version", default="v2", choices=list(LABEL_VERSIONS))
    train.add_argument("--variant", default="k15_b10", choices=sorted(VARIANT_BY_NAME))
    train.add_argument("--window-months", type=int, default=12)
    train.add_argument("--holdout-months", type=int, default=3)
    train.add_argument("--max-train-rows", type=int, default=500_000)
    train.add_argument("--min-train-rows", type=int, default=20_000)
    train.add_argument("--no-mlflow", action="store_true")
    train.add_argument("--no-save", action="store_true")

    precision = sub.add_parser(
        "precision", help="is the model's confidence meaningful, and do the strongest pay?"
    )
    dates(precision)
    precision.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    precision.add_argument("--universe", default="nifty50")
    precision.add_argument("--retrain", action="store_true", help="ignore the cache")
    precision.add_argument(
        "--labels", dest="label_version", default="v2", choices=list(ATR_LABEL_VERSIONS)
    )
    precision.add_argument("--variant", default="k15_b10", choices=sorted(VARIANT_BY_NAME))
    precision.add_argument("--window-months", type=int, default=12)
    precision.add_argument("--holdout-months", type=int, default=3)
    precision.add_argument("--max-train-rows", type=int, default=500_000)
    precision.add_argument("--min-train-rows", type=int, default=20_000)

    pick = sub.add_parser(
        "select-universe", help="choose a liquid, volatile intraday universe from daily candles"
    )
    pick.add_argument("--name", required=True)
    pick.add_argument("--size", type=int, default=universe.DEFAULT_SIZE)
    pick.add_argument("--source", default=universe.DEFAULT_SOURCE)
    pick.add_argument("--lookback-days", type=int, default=universe.DEFAULT_LOOKBACK_DAYS)
    pick.add_argument(
        "--min-turnover-crore", type=float, default=universe.DEFAULT_MIN_TURNOVER / universe.CRORE
    )
    pick.add_argument("--dry-run", action="store_true")

    shown = sub.add_parser("report", help="the backtests stored so far")
    shown.add_argument("--limit", type=int, default=40)

    replay = sub.add_parser("replay", help="step through one past day and show the trade plans")
    replay.add_argument(
        "--date",
        dest="day",
        type=date.fromisoformat,
        help="a past trading day; the last built day by default",
    )
    replay.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    replay.add_argument("--strategy", default="orb_15")
    replay.add_argument(
        "--labels", dest="label_version", default="v2", choices=list(LABEL_VERSIONS)
    )
    replay.add_argument("--variant", default="k15_b10", choices=sorted(VARIANT_BY_NAME))
    replay.add_argument("--universe", default="nifty50")

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
                if args.group in ("features", "labels"):
                    kind = "intraday_features" if args.group == "features" else "intraday_labels"
                    version = getattr(args, "version", VERSION)
                    params = {
                        "from": args.start and args.start.isoformat(),
                        "version": version,
                        "universe": args.universe,
                    }
                    with ingestion_run(
                        settings, f"{kind}_build", source="candles_intraday", params=params
                    ) as run:
                        summary = build(
                            settings,
                            conn,
                            kind,
                            start=args.start,
                            version=version,
                            universe=args.universe,
                            progress=lambda m: print(f"  {m} ...", flush=True),
                        )
                        run.add_rows(summary.rows)
                    print("\n".join(summary.lines()))
                elif args.group == "backtest":
                    from market_intel import intraday_run

                    out = intraday_run.run_backtest(
                        settings,
                        conn,
                        universe=args.universe,
                        capital=args.capital,
                        label_version=args.label_version,
                        variant=args.variant,
                        start=args.start,
                        end=args.end,
                        headline_slippage=(
                            args.slippage
                            if args.slippage is not None
                            else intraday_run.HEADLINE_SLIPPAGE
                        ),
                        save=not args.no_save,
                        progress=lambda m: print(f"  {m} ...", flush=True),
                    )
                    print("\n".join(out.lines()))
                elif args.group == "train":
                    from market_intel import intraday_backtest, intraday_models, intraday_run

                    config = intraday_models.TrainConfig(
                        label_version=args.label_version,
                        variant=args.variant,
                        window_months=args.window_months,
                        holdout_months=args.holdout_months,
                        max_train_rows=args.max_train_rows,
                        min_train_rows=args.min_train_rows,
                    )
                    with ingestion_run(
                        settings,
                        "intraday_train",
                        source="intraday_features",
                        params={"variant": args.variant, "labels": args.label_version},
                    ) as run:
                        result, runs, registered = intraday_run.run_train(
                            settings,
                            conn,
                            universe=args.universe,
                            capital=args.capital,
                            config=config,
                            use_mlflow=not args.no_mlflow,
                            save=not args.no_save,
                            start=args.start,
                            end=args.end,
                            progress=lambda m: print(f"  {m} ...", flush=True),
                        )
                        run.add_rows(len(result.predictions))
                    print("\n".join(result.lines()))
                    print()
                    print("\n".join(intraday_models.calibration_lines(result)))
                    if runs:
                        print()
                        print("\n".join(intraday_backtest.report_table(runs)))
                    for row in registered:
                        print(f"  registered {row['name']} v{row['version']} (candidate)")
                elif args.group == "precision":
                    from market_intel import intraday_models, intraday_run

                    print(
                        "\n".join(
                            intraday_run.run_precision(
                                settings,
                                conn,
                                universe=args.universe,
                                retrain=args.retrain,
                                capital=args.capital,
                                config=intraday_models.TrainConfig(
                                    label_version=args.label_version,
                                    variant=args.variant,
                                    window_months=args.window_months,
                                    holdout_months=args.holdout_months,
                                    max_train_rows=args.max_train_rows,
                                    min_train_rows=args.min_train_rows,
                                ),
                                start=args.start,
                                end=args.end,
                                progress=lambda m: print(f"  {m} ...", flush=True),
                            )
                        )
                    )
                elif args.group == "select-universe":
                    plan = universe.run_select(
                        conn,
                        name=args.name,
                        size=args.size,
                        source=args.source,
                        lookback_days=args.lookback_days,
                        min_turnover=args.min_turnover_crore * universe.CRORE,
                        dry_run=args.dry_run,
                    )
                    print("\n".join(plan.lines()))
                elif args.group == "report":
                    from market_intel import intraday_run

                    print("\n".join(intraday_run.run_report(conn, limit=args.limit)))
                elif args.group == "replay":
                    from market_intel import intraday_run

                    print(
                        "\n".join(
                            intraday_run.run_replay(
                                settings,
                                conn,
                                day=args.day,
                                capital=args.capital,
                                strategy=args.strategy,
                                label_version=args.label_version,
                                variant=args.variant,
                                universe=args.universe,
                            )
                        )
                    )
                else:
                    t0 = time_mod.monotonic()
                    report, total = check_real(
                        conn,
                        args.sample,
                        universe=args.universe,
                        label_version=args.label_version,
                    )
                    print("\n".join(report.lines()))
                    print(f"  samples {total}, duration {time_mod.monotonic() - t0:.0f} s")
                    log.info("intraday_leakage_checked", passed=report.passed, samples=total)
                    return 0 if report.passed else 1
        except (ConfigError, LeakageError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
