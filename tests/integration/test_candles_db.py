"""S1 as the app login (mi_app): migration 0006 (universe_members, price_events), the candle
upsert precedence, gaps and price events through the real SQL (PgStore).

Everything runs in ONE transaction that is always rolled back, on a fake instrument, so it is
safe on a real local database. Runs in CI compose-smoke (RUN_INTEGRATION=1). No network.
"""

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest

from market_intel.candles import PgStore
from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.sources.records import DailyCandle

pytestmark = pytest.mark.integration

D0 = date(2026, 11, 2)


def candle(source: str, day: date, close: str = "100") -> DailyCandle:
    price = Decimal(close)
    return DailyCandle(source=source, symbol="ZZCANDLE", trade_date=day, open=price, high=price,
                       low=price, close=price, volume=5, price_basis="raw")  # fmt: skip


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            yield c


@pytest.fixture
def inst_id(conn) -> int:
    return conn.execute(
        "INSERT INTO instruments (segment, symbol, is_active) VALUES ('EQ', 'ZZCANDLE', false)"
        " RETURNING id"
    ).fetchone()[0]


def test_0006_tables_as_the_app_login(conn, inst_id):
    assert conn.execute("SELECT current_user").fetchone()[0] == "mi_app"
    for table in ("universe_members", "price_events"):
        for right in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert conn.execute(
                "SELECT has_table_privilege(current_user, %s, %s)", (table, right)
            ).fetchone()[0], f"mi_app lacks {right} on {table}"
    conn.execute(
        "INSERT INTO universe_members (universe, instrument_id, as_of) VALUES ('zz_test', %s, %s)",
        (inst_id, D0),
    )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO price_events (instrument_id, trade_date, kind, ratio)"
            " VALUES (%s, %s, 'dividend', 1)",
            (inst_id, D0),
        )


def test_upsert_precedence_and_no_rewrite_of_unchanged_rows(conn, inst_id):
    store = PgStore(conn)
    assert (
        store.upsert(inst_id, [candle("yfinance", D0), candle("yfinance", D0 + timedelta(1))]) == 2
    )
    assert store.upsert(inst_id, [candle("yfinance", D0)]) == 0  # unchanged: not rewritten
    assert store.upsert(inst_id, [candle("upstox", D0, "101")]) == 1  # upstox replaces yfinance
    assert store.upsert(inst_id, [candle("yfinance", D0, "1")]) == 0  # never the reverse
    rows = conn.execute(
        "SELECT trade_date, source, close FROM candles_daily WHERE instrument_id = %s ORDER BY 1",
        (inst_id,),
    ).fetchall()
    assert rows == [
        (D0, "upstox", Decimal("101.0000")),
        (D0 + timedelta(1), "yfinance", Decimal("100.0000")),
    ]
    assert store.source_dates(inst_id, "yfinance") == [D0 + timedelta(1)]


def test_gaps_and_price_events(conn, inst_id):
    store = PgStore(conn)
    store.save_gaps(inst_id, [(D0, D0 + timedelta(2), "open")], [], None)
    store.save_gaps(inst_id, [(D0, D0 + timedelta(3), "unfillable")], [], None)  # updated
    [(gap_id, start, end, status)] = store.gaps(inst_id)
    assert (start, end, status) == (D0, D0 + timedelta(3), "unfillable")
    store.save_gaps(inst_id, [], [gap_id], None)
    assert store.gaps(inst_id)[0][3] == "filled"
    assert store.gap_counts([inst_id]) == {"filled": 1}
    assert store.add_price_events(inst_id, [(D0, Decimal("0.5"))]) == 1
    assert store.add_price_events(inst_id, [(D0, Decimal("0.5"))]) == 0  # once only
    assert store.price_event_count([inst_id]) == 1
