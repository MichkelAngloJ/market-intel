"""Seed a THROWAWAY database with synthetic market data for CI (compose-smoke): fake stocks in
two sectors (universe nifty500), a fake Nifty 50 and India VIX, ~400 business days of
random-walk candles, one special session and one suspected split.

    uv run python scripts/fake_market_data.py

Safety: it refuses to run if ``candles_daily`` already holds any row, so it can never mix fake
data into a real database. It uses the app login (POSTGRES_URL). Made-up values only.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import numpy as np

from market_intel.config import get_settings
from market_intel.db import postgres

DAYS = 400
STOCKS = [
    (f"ZZFEAT{i}", f"INE9ZZ{i:05d}0", "Fake Sector A" if i < 4 else "Fake Sector B")
    for i in range(8)
]
INDICES = [("NIFTY", "NSE_INDEX|Nifty 50", 20000.0), ("INDIA VIX", "NSE_INDEX|India VIX", 15.0)]


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(rng: np.random.Generator, n: int, start: float) -> list[tuple[float, ...]]:
    close = start * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    opn = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(opn, close) * 1.004
    low = np.minimum(opn, close) * 0.996
    vol = rng.integers(1_000, 90_000, n)
    return [
        (round(float(o), 4), round(float(h), 4), round(float(lo), 4), round(float(c), 4), int(v))
        for o, h, lo, c, v in zip(opn, high, low, close, vol, strict=True)
    ]


def main() -> int:
    settings = get_settings()
    rng = np.random.default_rng(42)
    days = business_days(date(2025, 1, 6), DAYS)
    special = days[200] + timedelta(days=(5 - days[200].weekday()))  # a Saturday session
    with postgres.connect(settings.require("database")) as conn:
        if conn.execute("SELECT EXISTS (SELECT 1 FROM candles_daily)").fetchone()[0]:
            print(
                "Refused: candles_daily is not empty (this script is for a throwaway CI database)."
            )
            return 1
        with conn.transaction():
            ids = {}
            for symbol, key, _ in INDICES:
                row = conn.execute(
                    "SELECT id FROM instruments WHERE instrument_key = %s", (key,)
                ).fetchone()
                ids[key] = (
                    row[0]
                    if row
                    else conn.execute(
                        "INSERT INTO instruments (segment, symbol, instrument_key, is_active)"
                        " VALUES ('INDEX', %s, %s, true) RETURNING id",
                        (symbol, key),
                    ).fetchone()[0]
                )
            for symbol, isin, sector in STOCKS:
                ids[symbol] = conn.execute(
                    "INSERT INTO instruments (segment, symbol, series, isin, instrument_key,"
                    " sector, is_active) VALUES ('EQ', %s, 'EQ', %s, %s, %s, true) RETURNING id",
                    (symbol, isin, f"NSE_EQ|{isin}", sector),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO universe_members (universe, instrument_id, as_of)"
                    " VALUES ('nifty500', %s, %s)",
                    (ids[symbol], days[-1]),
                )
            rows = []
            for _, key, start in INDICES:
                for d, c in zip(days, walk(rng, DAYS, start), strict=True):
                    rows.append((ids[key], d, *c))
                rows.append(
                    (ids[key], special, *walk(rng, 1, start)[0])
                )  # the index trades the special session
            for symbol, _, _ in STOCKS:
                candles = walk(rng, DAYS, 100.0)
                for i, (d, c) in enumerate(zip(days, candles, strict=True)):
                    if symbol == "ZZFEAT1" and i == 150:
                        continue  # a gap
                    if symbol == "ZZFEAT2" and i >= 300:
                        c = tuple(x / 2 if j < 4 else x for j, x in enumerate(c))  # a 1:2 split
                    rows.append((ids[symbol], d, *c))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO candles_daily (instrument_id, trade_date, open, high, low,"
                    " close, volume, price_basis, source)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, 'raw', 'upstox')",
                    rows,
                )
            conn.execute(
                "INSERT INTO price_events (instrument_id, trade_date, kind, ratio)"
                " VALUES (%s, %s, 'suspected_split_bonus', 0.5)",
                (ids["ZZFEAT2"], days[300]),
            )
    print(f"fake market data: {len(STOCKS)} stocks, {len(INDICES)} indices, {len(rows)} candles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
