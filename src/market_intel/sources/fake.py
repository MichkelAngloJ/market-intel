"""A fake data source for tests: deterministic made-up data, no network, offers every kind.

Its tag is ``fake``. The candle tables' check constraint refuses that tag, so fake candles can
never end up in the real tables.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from market_intel.sources.base import DataSource
from market_intel.sources.records import (
    FAKE_TAG,
    DailyCandle,
    Instrument,
    IntradayCandle,
    NewsRecord,
)

FEED_URL = "https://news.invalid/feed.xml"
SYMBOLS = ("DEMO_A", "DEMO_B")


class FakeSource(DataSource):
    def __init__(self, *, symbols: tuple[str, ...] = SYMBOLS, calls: list | None = None) -> None:
        super().__init__(FAKE_TAG)
        self.symbols = symbols
        self.calls = calls if calls is not None else []  # what was asked, for assertions

    def instruments(self) -> Iterator[Instrument]:
        self.calls.append(("instruments",))
        for i, symbol in enumerate(self.symbols, start=1):
            yield self.record(
                Instrument,
                segment="EQ",
                symbol=symbol,
                name=f"Demo company {i}",
                series="EQ",
                instrument_key=f"NSE_EQ|DEMO{i:07d}",
                tick_size=Decimal("0.05"),
                lot_size=1,
            )

    @staticmethod
    def _price(symbol: str, n: int) -> Decimal:
        return Decimal(100 + 10 * (sum(map(ord, symbol)) % 7) + n % 5)

    def daily_candles(self, symbol: str, start: date, end: date) -> Iterator[DailyCandle]:
        """One candle per weekday (no holidays in fake land)."""
        self.calls.append(("daily_candles", symbol, start, end))
        day = start
        while day <= end:
            if day.weekday() < 5:
                base = self._price(symbol, day.toordinal())
                yield self.record(
                    DailyCandle,
                    symbol=symbol,
                    trade_date=day,
                    open=base,
                    high=base + 2,
                    low=base - 1,
                    close=base + 1,
                    volume=1000 + day.day,
                    price_basis="raw",
                )
            day += timedelta(days=1)

    def intraday_candles(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[IntradayCandle]:
        self.calls.append(("intraday_candles", symbol, start, end))
        ts = start.astimezone(UTC).replace(second=0, microsecond=0)
        while ts < end:
            base = self._price(symbol, ts.minute)
            yield self.record(
                IntradayCandle,
                symbol=symbol,
                ts=ts,
                open=base,
                high=base + Decimal("0.5"),
                low=base - Decimal("0.5"),
                close=base,
                volume=10,
                price_basis="raw",
            )
            ts += timedelta(minutes=1)

    def news(self) -> Iterator[NewsRecord]:
        self.calls.append(("news",))
        for i, symbol in enumerate(self.symbols, start=1):
            yield self.record(
                NewsRecord,
                feed_url=FEED_URL,
                url=f"https://news.invalid/story-{i}?utm_source=feed",
                title=f"{symbol} wins a demo order",
                summary="Made-up news for tests.",
                published_at=datetime(2026, 11, 20, 4, 30, tzinfo=UTC),
                symbols=[symbol],
            )
