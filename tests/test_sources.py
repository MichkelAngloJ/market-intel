"""Task 1.0: the data-source interface and its typed records (no network)."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from market_intel.db import schema
from market_intel.mongo.models import NewsItem, url_hash
from market_intel.sources import (
    DailyCandle,
    DataSource,
    Instrument,
    IntradayCandle,
    NewsRecord,
    NotSupportedError,
    check_tag,
)
from market_intel.sources.fake import FakeSource

CANDLE = dict(
    symbol="DEMO",
    trade_date=date(2026, 11, 20),
    open="100.5",
    high="102",
    low="99.25",
    close="101",
    volume=1000,
    price_basis="raw",
)
IST = timezone(timedelta(hours=5, minutes=30))


# --- a record can't exist without its source ----------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "fields"),
    [
        (DailyCandle, CANDLE),
        (
            IntradayCandle,
            {**{k: v for k, v in CANDLE.items() if k != "trade_date"}, "ts": datetime.now(UTC)},
        ),
        (Instrument, {"segment": "EQ", "symbol": "DEMO"}),
        (
            NewsRecord,
            {
                "feed_url": "https://x.invalid/f",
                "url": "https://x.invalid/a",
                "title": "t",
                "published_at": datetime.now(UTC),
            },
        ),
    ],
)
def test_building_a_record_without_a_source_fails(cls, fields):
    with pytest.raises(ValidationError, match="source"):
        cls(**fields)
    with pytest.raises(ValidationError, match="source"):
        cls(**fields, source=None)


@pytest.mark.parametrize("tag", ["upstox", "yfinance", "rss:et_markets", "nse:announcements"])
def test_valid_source_tags(tag):
    assert check_tag(tag) == tag


@pytest.mark.parametrize("tag", ["", "Upstox", "rss:", "rss:Mint Feed", "a b", ":x", "x" * 40])
def test_invalid_source_tags(tag):
    with pytest.raises(ValueError, match="invalid source tag"):
        check_tag(tag)


def test_candle_source_must_be_one_the_tables_accept():
    assert DailyCandle(**CANDLE, source="upstox").source == "upstox"
    assert DailyCandle(**CANDLE, source="yfinance").source == "yfinance"
    with pytest.raises(ValidationError, match="not accepted by the candle tables"):
        DailyCandle(**CANDLE, source="rss:mint")


def test_records_are_immutable_and_refuse_unknown_fields():
    candle = DailyCandle(**CANDLE, source="upstox")
    with pytest.raises(ValidationError):
        candle.close = Decimal(1)  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        DailyCandle(**CANDLE, source="upstox", adj_close="1")


# --- records follow the tables' rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"low": "100.6"},  # low above open
        {"high": "100.9"},  # high below close
        {"volume": -1},
        {"price_basis": "split"},
        {"open": "1.00001"},  # more than 4 decimals: doesn't fit numeric(14,4)
        {"close": "12345678901.5"},  # more than 14 digits
    ],
)
def test_candle_rules(change):
    with pytest.raises(ValidationError):
        DailyCandle(**{**CANDLE, **change}, source="upstox")


def test_intraday_needs_an_aware_time_and_stores_utc():
    fields = {k: v for k, v in CANDLE.items() if k != "trade_date"}
    with pytest.raises(ValidationError):
        IntradayCandle(**fields, ts=datetime(2026, 11, 20, 9, 15), source="upstox")
    bar = IntradayCandle(**fields, ts=datetime(2026, 11, 20, 9, 15, tzinfo=IST), source="upstox")
    assert bar.ts == datetime(2026, 11, 20, 3, 45, tzinfo=UTC) and bar.ts.tzinfo == UTC
    assert bar.interval == "1m"


def test_candle_fields_match_the_candle_tables():
    # Everything a candle record holds has a column, except how the instrument is named
    # (symbol/exchange; storage maps them to instrument_id).
    naming = {"symbol", "exchange"}
    for cls, table, generated in (
        (DailyCandle, schema.candles_daily, {"instrument_id", "ingested_at"}),
        (IntradayCandle, schema.candles_intraday, {"instrument_id", "ingested_at"}),
    ):
        fields = set(cls.model_fields) - naming
        columns = set(table.columns.keys())
        assert fields == columns - generated, (cls.__name__, fields ^ (columns - generated))


def test_instrument_fields_match_the_instruments_table():
    fields = set(Instrument.model_fields) - {"source"}
    columns = set(schema.instruments.columns.keys()) - {"id", "created_at", "updated_at"}
    assert fields == columns


def test_news_record_becomes_a_valid_news_item():
    record = NewsRecord(
        source="rss:mint",
        feed_url="https://x.invalid/f",
        url="https://X.invalid/a/?utm_source=rss",
        title="t",
        published_at=datetime(2026, 11, 20, 10, 0, tzinfo=IST),
    )
    doc = record.to_document()
    assert isinstance(doc, NewsItem)
    assert doc.source == "rss:mint" and doc.url_hash == url_hash(record.url)
    assert doc.published_at == datetime(2026, 11, 20, 4, 30, tzinfo=UTC)


# --- the interface ------------------------------------------------------------------------------


class OnlyNews(DataSource):
    def __init__(self):
        super().__init__("rss:demo")

    def news(self):
        yield self.record(
            NewsRecord,
            feed_url="https://x.invalid/f",
            url="https://x.invalid/a",
            title="t",
            published_at=datetime.now(UTC),
        )


def test_unsupported_kinds_raise_and_supports_tells():
    src = OnlyNews()
    assert src.supports("news") and not src.supports("daily_candles")
    with pytest.raises(NotSupportedError, match="'rss:demo' does not offer daily_candles"):
        src.daily_candles("X", date(2026, 1, 1), date(2026, 1, 2))
    [item] = src.news()
    assert item.source == "rss:demo"


def test_a_source_cannot_build_records_for_another_source():
    with pytest.raises(ValueError, match="can't build a record for 'upstox'"):
        OnlyNews().record(NewsRecord, source="upstox")


def test_a_source_needs_a_valid_tag():
    with pytest.raises(ValueError, match="invalid source tag"):
        DataSource("Not Valid")


# --- the fake source end to end -----------------------------------------------------------------


def test_fake_source_end_to_end():
    calls = []
    src = FakeSource(calls=calls)
    assert all(
        src.supports(k) for k in ("instruments", "daily_candles", "intraday_candles", "news")
    )

    instruments = list(src.instruments())
    assert [i.symbol for i in instruments] == ["DEMO_A", "DEMO_B"]

    # Mon 2026-11-16 .. Sun 2026-11-22: five weekdays
    candles = list(src.daily_candles("DEMO_A", date(2026, 11, 16), date(2026, 11, 22)))
    assert [c.trade_date.weekday() for c in candles] == [0, 1, 2, 3, 4]

    start = datetime(2026, 11, 20, 9, 15, tzinfo=IST)
    bars = list(src.intraday_candles("DEMO_B", start, start + timedelta(minutes=5)))
    assert len(bars) == 5 and bars[0].ts == start.astimezone(UTC)

    docs = [n.to_document() for n in src.news()]
    assert len({d.url_hash for d in docs}) == 2

    records = [*instruments, *candles, *bars, *src.news()]
    assert {r.source for r in records} == {"fake"}
    assert calls[0] == ("instruments",) and calls[1][0] == "daily_candles"


def test_fake_candles_can_never_be_stored():
    # The fake tag passes the record check but not the tables' check constraint.
    assert "fake" not in schema.CANDLE_SOURCES
