"""Postgres schema v1 as SQLAlchemy Core tables: the source of truth for Alembic.

Change a table here, then add a migration (``alembic revision --autogenerate``) and review it
by hand. CI runs ``alembic check`` to prove this file and the migrations still match.

General rules:
- Timestamps are ``timestamptz`` and stored in UTC.
- Trading days are ``date``: the exchange date in IST.
- Prices are ``numeric(14,4)`` (exact decimals, no float rounding).
- Every candle row records its ``source`` and whether its prices are ``raw`` or ``adjusted``.

Tables are designed so they could become TimescaleDB hypertables later: the time column is
part of every candle primary key.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# Every constraint and index gets a predictable name, so migrations can find it later.
# (Autogenerate can only compare constraints that have names.)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

PRICE = Numeric(14, 4)
TIMESTAMPTZ = TIMESTAMP(timezone=True)
NOW = text("now()")

PRICE_BASES = ("raw", "adjusted")
CANDLE_SOURCES = ("upstox", "yfinance")
RUN_STATUSES = ("running", "success", "partial", "failed")
GAP_INTERVALS = ("1d", "1m")
GAP_STATUSES = ("open", "filled", "unfillable")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _candle_columns() -> list:
    """Price, volume and provenance columns shared by both candle tables."""
    return [
        Column("open", PRICE, nullable=False),
        Column("high", PRICE, nullable=False),
        Column("low", PRICE, nullable=False),
        Column("close", PRICE, nullable=False),
        Column("volume", BigInteger, nullable=False),
        Column("oi", BigInteger),  # open interest; derivatives only
        Column("price_basis", Text, nullable=False),
        Column("source", Text, nullable=False),
        Column("ingested_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    ]


def _candle_checks() -> list:
    return [
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint(
            "low <= LEAST(open, close) AND high >= GREATEST(open, close)", name="ohlc_range"
        ),
        CheckConstraint(_in("price_basis", PRICE_BASES), name="price_basis"),
        CheckConstraint(_in("source", CANDLE_SOURCES), name="source"),
    ]


instruments = Table(
    "instruments",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("exchange", Text, nullable=False, server_default="NSE"),
    Column("segment", Text, nullable=False),  # EQ, INDEX, ...
    Column("symbol", Text, nullable=False),
    # Postgres UNIQUE allows any number of NULLs: "unique when not null".
    Column("isin", Text, unique=True),
    Column("name", Text),
    Column("series", Text),
    Column("instrument_key", Text, unique=True),  # Upstox instrument key
    Column("sector", Text),
    Column("industry", Text),
    Column("tick_size", PRICE),
    Column("lot_size", Integer),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("listed_on", Date),
    Column("delisted_on", Date),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=NOW),  # set by the app
    UniqueConstraint("exchange", "segment", "symbol"),
)

# One row per instrument per trading day.
# Source precedence: an 'upstox' row may replace a 'yfinance' row for the same day, never the
# reverse. The Phase 1 loader enforces this (the database does not).
candles_daily = Table(
    "candles_daily",
    metadata,
    Column("instrument_id", BigInteger, ForeignKey("instruments.id"), nullable=False),
    Column("trade_date", Date, nullable=False),  # IST exchange date
    *_candle_columns(),
    PrimaryKeyConstraint("instrument_id", "trade_date"),
    *_candle_checks(),
    # BRIN: a tiny index that works well because rows arrive in date order.
    Index(None, "trade_date", postgresql_using="brin"),
)

# 1-minute bars only; other intervals are computed from these.
candles_intraday = Table(
    "candles_intraday",
    metadata,
    Column("instrument_id", BigInteger, ForeignKey("instruments.id"), nullable=False),
    Column("ts", TIMESTAMPTZ, nullable=False),  # bar start, UTC
    Column("interval", Text, nullable=False),
    *_candle_columns(),
    PrimaryKeyConstraint("instrument_id", "interval", "ts"),
    *_candle_checks(),
    CheckConstraint("interval = '1m'", name="interval"),
    Index(None, "ts", postgresql_using="brin"),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("job", Text, nullable=False),
    Column("source", Text),
    Column("started_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    Column("finished_at", TIMESTAMPTZ),
    Column("status", Text, nullable=False, server_default="running"),
    Column("rows_written", BigInteger, nullable=False, server_default="0"),
    Column("params", JSONB),
    Column("error", Text),  # callers must store the MASKED error text
    CheckConstraint(_in("status", RUN_STATUSES), name="status"),
    CheckConstraint("rows_written >= 0", name="rows_written_non_negative"),
)

data_gaps = Table(
    "data_gaps",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("instrument_id", BigInteger, ForeignKey("instruments.id"), nullable=False),
    Column("interval", Text, nullable=False),
    Column("gap_start", TIMESTAMPTZ, nullable=False),
    Column("gap_end", TIMESTAMPTZ, nullable=False),
    Column("detected_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    Column("status", Text, nullable=False, server_default="open"),
    Column("filled_by_run_id", BigInteger, ForeignKey("ingestion_runs.id")),
    Column("note", Text),
    UniqueConstraint("instrument_id", "interval", "gap_start"),
    CheckConstraint(_in("interval", GAP_INTERVALS), name="interval"),
    CheckConstraint(_in("status", GAP_STATUSES), name="status"),
    CheckConstraint("gap_end >= gap_start", name="gap_order"),
)

llm_spend = Table(
    "llm_spend",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("called_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text),
    Column("purpose", Text),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", Numeric(12, 6)),
    Column("usd_inr", Numeric(10, 4)),
    Column("cost_inr", Numeric(12, 4), nullable=False),  # what the monthly cap counts
    Column("run_id", BigInteger, ForeignKey("ingestion_runs.id")),
    CheckConstraint("cost_inr >= 0", name="cost_inr_non_negative"),
    Index(None, "called_at"),
)

# Views are not tracked by autogenerate; migration 0001 creates this one with op.execute.
# Months are IST calendar months, matching the Rs 1,000/month LLM cap.
V_LLM_SPEND_MONTHLY = "v_llm_spend_monthly"
