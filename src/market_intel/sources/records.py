"""Typed records that every data source returns. Each record carries its source tag, and can't
be built without one.

They mirror the storage they're meant for:
- ``Instrument``      -> Postgres ``instruments`` (the table has no source column; the tag says
                         where the row came from, for logs and ingestion runs)
- ``DailyCandle``     -> ``candles_daily``
- ``IntradayCandle``  -> ``candles_intraday``
- ``NewsRecord``      -> MongoDB ``news_items`` (``to_document()`` builds the validated model)

Sources don't know the database's ``instruments.id``; candles name their instrument by
``exchange`` and ``symbol``, and the storage code (from 1.1) maps that to the id.

Values follow the tables' rules, so a bad row fails here, before it reaches the database:
prices fit ``numeric(14,4)``, ``low <= min(open, close)``, ``high >= max(open, close)``,
volume >= 0, timestamps timezone-aware (stored in UTC), and the price basis is raw or adjusted.
Candle sources must be ones the candle tables accept (``upstox``, ``yfinance``). The fake
source's tag ``fake`` is also accepted so tests can run end to end; the tables' check
constraint refuses it, so fake candles can never be stored.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from market_intel.db.schema import CANDLE_SOURCES, PRICE_BASES
from market_intel.mongo.models import (
    ExchangeMeta,
    NewsItem,
    NewsKind,
    PublishedAtSource,
    UtcDatetime,
    utc_now,
)

FAKE_TAG = "fake"

# "upstox", "yfinance", "rss:et_markets", "nse:announcements", "fake"
_TAG = re.compile(r"^[a-z][a-z0-9_]{0,31}(:[a-z0-9][a-z0-9._-]{0,63})?$")


def check_tag(tag: str) -> str:
    """A stable source tag: a lower-case name, optionally ``:<feed>`` (e.g. ``rss:mint``)."""
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ValueError(
            f"invalid source tag {tag!r}: use a lower-case name like 'upstox' or 'rss:<feed>'"
        )
    return tag


def _candle_source(tag: str) -> str:
    check_tag(tag)
    if tag not in (*CANDLE_SOURCES, FAKE_TAG):
        raise ValueError(
            f"candle source {tag!r} is not accepted by the candle tables "
            f"(allowed: {', '.join(CANDLE_SOURCES)})"
        )
    return tag


SourceTag = Annotated[str, AfterValidator(check_tag)]
CandleSource = Annotated[str, AfterValidator(_candle_source)]
Price = Annotated[Decimal, Field(max_digits=14, decimal_places=4, ge=0)]
PriceBasis = Literal["raw", "adjusted"]
assert set(PriceBasis.__args__) == set(PRICE_BASES)  # type: ignore[attr-defined]


class Record(BaseModel):
    """Base of every record: immutable, no unknown fields, and a required source tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceTag  # no default: a record without its source can't exist


class Instrument(Record):
    exchange: str = "NSE"
    segment: str
    symbol: str = Field(min_length=1)
    isin: str | None = None
    name: str | None = None
    series: str | None = None
    instrument_key: str | None = None  # the broker's id (Upstox: e.g. "NSE_EQ|INE...")
    sector: str | None = None
    industry: str | None = None
    tick_size: Price | None = None
    lot_size: int | None = Field(default=None, ge=1)
    is_active: bool = True
    listed_on: date | None = None
    delisted_on: date | None = None
    # Set by the instrument refresh (IST dates), never by a source: left None in records.
    first_seen_on: date | None = None
    last_seen_on: date | None = None


class _Candle(Record):
    source: CandleSource
    exchange: str = "NSE"
    symbol: str = Field(min_length=1)
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int = Field(ge=0)
    oi: int | None = Field(default=None, ge=0)  # open interest; derivatives only
    price_basis: PriceBasis

    @model_validator(mode="after")
    def _ohlc_range(self) -> _Candle:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("needs low <= min(open, close) and high >= max(open, close)")
        return self


class DailyCandle(_Candle):
    trade_date: date  # the exchange date (IST)


class IntradayCandle(_Candle):
    ts: UtcDatetime  # start of the bar; naive times are refused
    interval: Literal["1m"] = "1m"


class NewsRecord(Record):
    """One news item as a source returns it; ``to_document()`` gives the MongoDB model."""

    feed_url: str
    url: str
    title: str = Field(min_length=1)
    summary: str | None = None
    published_at: UtcDatetime  # when the source says it became public
    fetched_at: UtcDatetime = Field(default_factory=utc_now)
    published_at_source: PublishedAtSource = "feed"
    kind: NewsKind = "news"
    publisher: str | None = None
    feed_name: str | None = None
    exchange_meta: ExchangeMeta | None = None
    language: str = "en"
    symbols: list[str] = Field(default_factory=list)

    def to_document(self) -> NewsItem:
        """The validated MongoDB model; ``available_at`` = max(published_at, fetched_at)."""
        return NewsItem(**self.model_dump())
