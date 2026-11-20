"""The one interface every data source implements.

A source has a stable ``tag`` ("upstox", "yfinance", "rss:<feed>", ...) and offers some of
four kinds of data. What it doesn't offer raises ``NotSupportedError``. Records are built with
``self.record(...)``, which fills in the source's own tag, so every row says where it came from.

Sources only fetch and return records: they never write to a database. Jobs call them inside
``ingestion_run(...)`` and through the source's rate limiter (market_intel.rate_limit).
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal, TypeVar

from market_intel.sources.records import (
    DailyCandle,
    Instrument,
    IntradayCandle,
    NewsRecord,
    Record,
    check_tag,
)

Kind = Literal["instruments", "daily_candles", "intraday_candles", "news"]
KINDS: tuple[Kind, ...] = ("instruments", "daily_candles", "intraday_candles", "news")
R = TypeVar("R", bound=Record)


class NotSupportedError(NotImplementedError):
    """The source doesn't offer this kind of data."""


class DataSource(ABC):  # noqa: B024 - no method is required; each source offers what it has
    """Base class of every data source. Override the methods for the kinds you offer."""

    def __init__(self, tag: str) -> None:
        self._tag = check_tag(tag)

    @property
    def tag(self) -> str:
        """The source tag stored with every row (stable: don't rename a live source)."""
        return self._tag

    def record(self, cls: type[R], **fields: Any) -> R:
        """Build a record tagged with this source. A different ``source`` is refused."""
        if fields.setdefault("source", self.tag) != self.tag:
            raise ValueError(f"source {self.tag!r} can't build a record for {fields['source']!r}")
        return cls(**fields)

    def supports(self, kind: Kind) -> bool:
        method = getattr(type(self), kind)
        return method is not getattr(DataSource, kind)

    def _unsupported(self, kind: str) -> NotSupportedError:
        return NotSupportedError(f"source {self.tag!r} does not offer {kind}")

    def instruments(self) -> Iterable[Instrument]:
        raise self._unsupported("instruments")

    def daily_candles(self, symbol: str, start: date, end: date) -> Iterable[DailyCandle]:
        """Daily candles for ``symbol`` from ``start`` to ``end`` (both included)."""
        raise self._unsupported("daily_candles")

    def intraday_candles(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterable[IntradayCandle]:
        """1-minute candles for ``symbol`` with ``start <= ts < end`` (aware datetimes)."""
        raise self._unsupported("intraday_candles")

    def news(self) -> Iterable[NewsRecord]:
        raise self._unsupported("news")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(tag={self.tag!r})"
