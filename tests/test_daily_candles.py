"""Daily candle sources (S1 parts B and C): Upstox V3 parsing, chunks and URL; the yfinance
backup with an honest session (never a browser identity) and raw prices. No network: a fake
Upstox client and a fake yfinance download."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from market_intel.rate_limit import RateLimiter, Window
from market_intel.sources import daily_candles as dc


def payload(*rows):
    return {"status": "success", "data": {"candles": [list(r) for r in rows]}}


def test_upstox_rows_become_raw_upstox_candles_on_the_ist_date():
    batch = dc.parse_upstox(
        payload(
            ("2026-11-20T00:00:00+05:30", 101.5, 104.0, 100.25, 103.12345, 1500, 0),
            ("2026-11-21T00:00:00+05:30", 103, 102, 101, 103, 10, 0),  # high < open: invalid
            ("not a time", 1, 1, 1, 1, 1, 0),
        ),
        "FAKEA",
    )
    [c] = batch.candles
    assert (c.source, c.price_basis, c.symbol, c.trade_date) == (
        "upstox",
        "raw",
        "FAKEA",
        date(2026, 11, 20),
    )
    assert (c.open, c.high, c.low, c.close, c.volume) == (
        Decimal("101.5000"), Decimal("104.0000"), Decimal("100.2500"), Decimal("103.1235"), 1500,
    )  # fmt: skip
    assert batch.malformed == 2


def test_an_unexpected_upstox_response_is_a_source_error():
    with pytest.raises(dc.CandleSourceError):
        dc.parse_upstox({"status": "error", "errors": []}, "FAKEA")
    with pytest.raises(dc.CandleSourceError):
        dc.parse_upstox({"status": "success", "data": {}}, "FAKEA")


def test_ranges_are_split_into_chunks_under_a_decade():
    chunks = list(dc.date_chunks(date(2015, 1, 1), date(2026, 11, 24)))
    assert chunks[0][1] == date(2026, 11, 24) and chunks[-1][0] == date(2015, 1, 1)
    assert all((to - frm).days < 3650 for frm, to in chunks) and len(chunks) == 2


class FakeUpstox:
    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        to = path.rsplit("/", 2)[1]
        return payload((f"{to}T00:00:00+05:30", 10, 11, 9, 10.5, 100, 0))


def test_fetch_upstox_encodes_the_key_and_asks_to_then_from():
    client = FakeUpstox()
    batch = dc.fetch_upstox(
        client, "NSE_INDEX|Nifty 50", "NIFTY", date(2015, 1, 1), date(2026, 11, 24)
    )
    assert (
        client.paths[0]
        == "/v3/historical-candle/NSE_INDEX%7CNifty%2050/days/1/2026-11-24/2017-11-27"
    )
    assert batch.requests == 2 and [c.trade_date for c in batch.candles] == sorted(
        c.trade_date for c in batch.candles
    )


# --- yfinance ----------------------------------------------------------------------------


def frame(rows):
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows], name="Date")
    data = {
        "Adj Close": [r[4] * 0.9 for r in rows],  # adjusted: must NOT be used
        "Close": [r[4] for r in rows],
        "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows],
        "Open": [r[1] for r in rows],
        "Volume": [r[5] for r in rows],
    }
    return pd.DataFrame(data, index=index)


def no_wait():
    return RateLimiter("t", [Window(1000, 1)], headroom=1.0)


def test_yfinance_gets_raw_prices_an_exclusive_end_and_an_honest_session():
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return frame(
            [
                ("2026-11-20", 100, 105, 99, 104, 5000),
                ("2026-11-21", 104, 106, 103, float("nan"), 10),
            ]
        )

    batch = dc.fetch_yfinance(
        "FAKEA", date(2026, 11, 20), date(2026, 11, 21), download=download, limiter=no_wait()
    )
    [kw] = calls
    assert kw["tickers"] == "FAKEA.NS" and kw["start"] == "2026-11-20" and kw["end"] == "2026-11-22"
    assert kw["auto_adjust"] is False and kw["interval"] == "1d" and kw["progress"] is False
    session = kw["session"]
    assert type(session).__module__.startswith("requests")  # not curl_cffi (no impersonation)
    agent = session.headers["User-Agent"]
    assert agent.startswith("market-intel/") and "Mozilla" not in agent and "Chrome" not in agent
    [c] = batch.candles  # the NaN close row is skipped
    assert (c.source, c.price_basis, c.close) == ("yfinance", "raw", Decimal("104.0000"))
    assert batch.malformed == 1


def test_yfinance_failures_and_empty_results_are_source_errors():
    def boom(**kwargs):
        raise RuntimeError("Too Many Requests")

    with pytest.raises(dc.CandleSourceError, match="Too Many Requests"):
        dc.fetch_yfinance(
            "FAKEA", date(2026, 11, 20), date(2026, 11, 21), download=boom, limiter=no_wait()
        )
    with pytest.raises(dc.CandleSourceError, match="no data"):
        dc.fetch_yfinance(
            "FAKEA", date(2026, 11, 20), date(2026, 11, 21),
            download=lambda **k: pd.DataFrame(), limiter=no_wait(),
        )  # fmt: skip
