"""Daily candles: Upstox V3 historical candles (primary) and yfinance (stock-only backup).

Upstox (checked 2026-11-24): ``GET /v3/historical-candle/{instrument_key}/days/1/{to}/{from}``
(dates inclusive, YYYY-MM-DD; at most one decade per request, from Jan 2000). Response:
``{"status": "success", "data": {"candles": [[ts, open, high, low, close, volume, oi], ...]}}``
with ``ts`` like ``2025-01-01T00:00:00+05:30``. Requests are split into chunks of at most 9
years. Records: ``source="upstox"``, ``price_basis="raw"``.

yfinance (1.8.0, checked 2026-11-24): ``download("<SYMBOL>.NS", start, end)`` with ``end``
EXCLUSIVE and ``auto_adjust=False`` for raw OHLCV. yfinance normally impersonates a browser
(curl_cffi, or a Chrome user agent); this project never does, so every call gets our own
``requests.Session`` with the honest user agent. Yahoo may refuse that; then the backup simply
fails and source health reports it. Stocks only (indices: Upstox only). Records:
``source="yfinance"``, ``price_basis="raw"``.

Rows that break the candle rules (OHLC range, negative volume, ...) are skipped and counted.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from market_intel import __version__
from market_intel.rate_limit import RateLimiter, Window
from market_intel.sources.records import DailyCandle

CHUNK_DAYS = 9 * 365  # under Upstox's one-decade limit per request
UPSTOX_EARLIEST = date(2000, 1, 1)
FOUR_PLACES = Decimal("0.0001")
USER_AGENT = f"market-intel/{__version__} (personal research)"
YAHOO_MIN_SECONDS = 2.0


class CandleSourceError(Exception):
    """The source failed for this instrument and range."""


@dataclass
class CandleBatch:
    candles: list[DailyCandle] = field(default_factory=list)
    malformed: int = 0
    requests: int = 0
    duplicates: int = 0  # extra candles for a day that already had one (Upstox, 2017-2018)


def _price(value: Any) -> Decimal:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise ValueError("missing price")
    try:
        return Decimal(str(value)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("bad price") from None


def _volume(value: Any) -> int:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0
    return int(value)


def date_chunks(start: date, end: date, days: int = CHUNK_DAYS) -> Iterator[tuple[date, date]]:
    """(from, to) ranges covering start..end inclusive, newest first, each <= ``days``."""
    to = end
    while to >= start:
        frm = max(start, to - timedelta(days=days - 1))
        yield frm, to
        to = frm - timedelta(days=1)


# --- Upstox ------------------------------------------------------------------------------


def parse_upstox(payload: Any, symbol: str) -> CandleBatch:
    """Parse one V3 response into raw daily candles (invalid rows counted, not raised).

    Upstox sometimes returns a day twice (seen 2026-11-24 for many indices in 2017-2018: one
    candle stamped 00:00 and one stamped 09:15 or 17:30). The 00:00 candle, the documented daily
    format, is kept; the others are counted as duplicates."""
    batch = CandleBatch()
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise CandleSourceError("unexpected Upstox response (status is not success)")
    rows = (payload.get("data") or {}).get("candles")
    if not isinstance(rows, list):
        raise CandleSourceError("unexpected Upstox response (no candles list)")
    chosen: dict[date, tuple[DailyCandle, bool]] = {}
    for row in rows:
        try:
            ts, o, h, low, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            stamp = datetime.fromisoformat(ts)
            trade_date = stamp.date()  # the +05:30 local (IST) date
            midnight = stamp.time() == datetime.min.time()
            candle = DailyCandle(
                source="upstox",
                symbol=symbol,
                trade_date=trade_date,
                open=_price(o),
                high=_price(h),
                low=_price(low),
                close=_price(c),
                volume=_volume(v),
                price_basis="raw",
            )
        except (ValidationError, ValueError, TypeError, IndexError):
            batch.malformed += 1
            continue
        previous = chosen.get(trade_date)
        if previous is not None:
            batch.duplicates += 1
            if previous[1] or not midnight:
                continue  # keep the 00:00 candle, else the first one
        chosen[trade_date] = (candle, midnight)
    batch.candles = [chosen[d][0] for d in sorted(chosen)]
    return batch


def fetch_upstox(
    client: Any, instrument_key: str, symbol: str, start: date, end: date
) -> CandleBatch:
    """All daily candles start..end (inclusive) from Upstox, in chunks. ``client`` is an
    :class:`market_intel.upstox.UpstoxClient` (its errors propagate)."""
    out = CandleBatch()
    key = quote(instrument_key, safe="")
    for frm, to in date_chunks(max(start, UPSTOX_EARLIEST), end):
        payload = client.get(
            f"/v3/historical-candle/{key}/days/1/{to.isoformat()}/{frm.isoformat()}"
        )
        part = parse_upstox(payload, symbol)
        out.candles += part.candles
        out.malformed += part.malformed
        out.duplicates += part.duplicates
        out.requests += 1
    out.candles.sort(key=lambda c: c.trade_date)
    return out


# --- yfinance ----------------------------------------------------------------------------

_yahoo_limiter = RateLimiter("yfinance", [Window(1, YAHOO_MIN_SECONDS)], headroom=1.0)


def honest_session() -> Any:
    """A plain requests Session with the project's user agent (never a browser's)."""
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _load_yfinance() -> Any:
    """Import yfinance with its curl_cffi backend (browser TLS impersonation) switched off: the
    second guard (S1 review); the first is our own honest session on every call. The switch
    is read when yfinance is first imported, which only happens here."""
    os.environ["YF_DISABLE_CURL_CFFI"] = "1"
    import yfinance

    return yfinance


def _yf_download(**kwargs: Any) -> Any:
    return _load_yfinance().download(**kwargs)


def fetch_yfinance(
    symbol: str,
    start: date,
    end: date,
    *,
    download: Callable[..., Any] = _yf_download,
    limiter: RateLimiter | None = None,
    session: Any | None = None,
) -> CandleBatch:
    """Raw daily candles start..end (inclusive) for an NSE stock from Yahoo ("<SYMBOL>.NS")."""
    (limiter or _yahoo_limiter).acquire()
    try:
        frame = download(
            tickers=f"{symbol}.NS",
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
            multi_level_index=False,
            session=session or honest_session(),
        )
    except Exception as exc:  # yfinance raises many kinds; all mean "this source failed"
        raise CandleSourceError(f"yfinance: {type(exc).__name__}: {exc}") from None
    batch = CandleBatch(requests=1)
    if frame is None or len(frame) == 0:
        raise CandleSourceError("yfinance returned no data")
    for index, row in frame.iterrows():
        try:
            trade_date = index.date() if hasattr(index, "date") else date.fromisoformat(str(index))
            batch.candles.append(
                DailyCandle(
                    source="yfinance",
                    symbol=symbol,
                    trade_date=trade_date,
                    open=_price(row["Open"]),
                    high=_price(row["High"]),
                    low=_price(row["Low"]),
                    close=_price(row["Close"]),
                    volume=_volume(row["Volume"]),
                    price_basis="raw",
                )
            )
        except (ValidationError, ValueError, TypeError, KeyError):
            batch.malformed += 1
    batch.candles = [c for c in batch.candles if start <= c.trade_date <= end]
    return batch
