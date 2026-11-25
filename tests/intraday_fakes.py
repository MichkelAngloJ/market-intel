"""Synthetic 1-minute bars for the intraday feature, label and leakage tests (made-up prices,
no real data): a few stocks, a fake Nifty 50, a fake sector index and India VIX."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from market_intel.intraday import IntradayInputs

IST = ZoneInfo("Asia/Kolkata")


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def minute_bars(days: list[date], seed: int, start: float = 100.0, *,
                bars_per_day: int = 375, sigma: float = 0.0008) -> pd.DataFrame:  # fmt: skip
    """Random-walk 1-minute bars 09:15..15:29 IST (or the first ``bars_per_day``)."""
    rng = np.random.default_rng(seed)
    frames = []
    price = start
    for d in days:
        base = datetime.combine(d, time(9, 15), IST)
        idx = pd.DatetimeIndex(
            [base + timedelta(minutes=m) for m in range(bars_per_day)]
        ).tz_convert("UTC")
        steps = rng.normal(0, sigma, bars_per_day)
        close = price * np.exp(np.cumsum(steps))
        opn = np.concatenate([[price * np.exp(rng.normal(0, 0.003))], close[:-1]])
        high = np.maximum(opn, close) * (1 + rng.uniform(0, 0.0008, bars_per_day))
        low = np.minimum(opn, close) * (1 - rng.uniform(0, 0.0008, bars_per_day))
        u = np.abs(np.arange(bars_per_day) - 187) / 187  # U-shaped volume through the day
        vol = (rng.integers(500, 5_000, bars_per_day) * (1 + 2 * u**2)).astype(float)
        frames.append(
            pd.DataFrame(
                {"open": opn, "high": high, "low": low, "close": close, "volume": vol}, index=idx
            )
        )
        price = float(close[-1])
    out = pd.concat(frames)
    out.index.name = "ts"
    return out


def daily_from(bars: pd.DataFrame) -> pd.DataFrame:
    local = bars.index.tz_convert(IST).date
    g = bars.groupby(local)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last()})  # fmt: skip
    out.index.name = "trade_date"
    return out


def inputs(n_days: int = 30, *, thin_day: int | None = None) -> IntradayInputs:
    days = business_days(date(2024, 3, 4), n_days)
    bars = {1: minute_bars(days, 1), 2: minute_bars(days, 2, 250.0), 3: minute_bars(days, 3, 50.0)}
    if thin_day is not None:  # instrument 3 has only 200 bars that day
        local = bars[3].index.tz_convert(IST)
        drop = (local.date == days[thin_day]) & (
            local.hour * 60 + local.minute >= 9 * 60 + 15 + 200
        )
        bars[3] = bars[3][~drop]
    market = minute_bars(days, 10, 22_000.0, sigma=0.0004)
    sector = minute_bars(days, 11, 40_000.0, sigma=0.0005)
    vix = minute_bars(days, 12, 14.0, sigma=0.001)
    return IntradayInputs(
        bars=bars,
        market=market,
        vix=vix,
        sectors={"Fake Sector": sector},
        sector_of={1: "Fake Sector", 2: "Fake Sector", 3: None},
        daily={k: daily_from(v) for k, v in bars.items()},
        daily_vix=daily_from(vix),
        events={2: {days[10]}},
        special=set(),
        symbols={1: "ZZA", 2: "ZZB", 3: "ZZC"},
    )


def at(day: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(hh, mm), IST)).tz_convert("UTC")
