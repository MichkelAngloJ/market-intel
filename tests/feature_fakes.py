"""Synthetic market data for the feature, label and leakage tests (made-up prices, no real
data): business days, random-walk candles for a few stocks in two sectors, Nifty 50 and VIX."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from market_intel.features import Inputs, NewsItem


def business_days(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def candles(days: list[date], seed: int, start_price: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(0, 0.015, len(days))))
    opn = close * np.exp(rng.normal(0, 0.005, len(days)))
    high = np.maximum(opn, close) * (1 + rng.uniform(0, 0.01, len(days)))
    low = np.minimum(opn, close) * (1 - rng.uniform(0, 0.01, len(days)))
    volume = rng.integers(1_000, 100_000, len(days)).astype(float)
    return pd.DataFrame(
        {"open": opn, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.Index(days, name="trade_date"),
    )


def inputs(n_days: int = 320) -> Inputs:
    days = business_days(date(2024, 1, 1), n_days)
    stocks = {
        10: candles(days, 1),
        11: candles(days[40:], 2),
        12: candles(days, 3),
        13: candles(days, 4),
    }
    # instrument 12 misses two days (gaps); instrument 13 has a "split" event
    stocks[12] = stocks[12].drop(index=[days[100], days[101]])
    news = {
        10: [
            NewsItem("exchange", datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=6))
            for d in days[::7]
        ]
        + [
            NewsItem(
                "company_news", datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=12)
            )
            for d in days[::5]
        ],
    }
    return Inputs(
        candles=stocks,
        sector_of={
            10: "Fake Sector A",
            11: "Fake Sector A",
            12: "Fake Sector B",
            13: "Fake Sector B",
        },
        symbols={10: "FAKEA", 11: "FAKEB", 12: "FAKEC", 13: "FAKED"},
        nifty=candles(days, 99, 20000.0),
        vix=candles(days, 98, 15.0),
        events={13: [days[150]]},
        news=news,
        calendar=list(days),
        special=[],
    )
