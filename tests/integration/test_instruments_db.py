"""Migration 0004, the instrument refresh SQL and the watchlist, as the app login (mi_app).

Every test runs inside ONE transaction that is always rolled back, so nothing is left behind,
also on a real local database that already holds the real instrument list. Inside it, the real
managed instruments are first made inactive (rolled back too), so the fake ones never meet the
safety valve or a real symbol. Fake ISINs start with "ZZ" (no such country code).

Runs in CI compose-smoke after "alembic upgrade head" (RUN_INTEGRATION=1).
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from market_intel import watchlist
from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.instruments import MANAGED_PREFIXES, refresh_in_transaction
from market_intel.sources.records import Instrument

pytestmark = pytest.mark.integration

DAY1, DAY2 = date(2026, 11, 22), date(2026, 11, 23)


def fake(symbol: str, n: int, series: str | None = "EQ") -> Instrument:
    if series is None:
        return Instrument(
            source="upstox",
            segment="INDEX",
            symbol=symbol,
            name=f"{symbol} index",
            instrument_key=f"NSE_INDEX|{symbol}",
        )
    isin = f"ZZTEST{n:05d}1"
    return Instrument(
        source="upstox",
        segment="EQ",
        symbol=symbol,
        name=f"{symbol} FAKE LIMITED",
        series=series,
        isin=isin,
        instrument_key=f"NSE_EQ|{isin}",
        tick_size=Decimal("0.05"),
        lot_size=1,
    )


FILE = [fake("ZZFAKEA", 1), fake("ZZFAKEB", 2, "BE"), fake("ZZ FAKE 50", 0, None)]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            c.execute(
                "UPDATE instruments SET is_active = false"
                " WHERE split_part(instrument_key, '|', 1) = ANY(%s)",
                (list(MANAGED_PREFIXES),),
            )
            c.execute("DELETE FROM watchlist")
            yield c


def refresh(conn, items, today=DAY1):
    return refresh_in_transaction(conn, items, today=today, min_keep_ratio=0.8)


def row(conn, symbol):
    return conn.execute(
        "SELECT id, series, is_active, first_seen_on, last_seen_on FROM instruments"
        " WHERE symbol = %s ORDER BY is_active DESC, id DESC LIMIT 1",
        (symbol,),
    ).fetchone()


def test_0004_as_the_app_login(conn):
    assert conn.execute("SELECT current_user").fetchone()[0] == "mi_app"
    assert conn.execute("SELECT to_regclass('watchlist') IS NOT NULL").fetchone()[0]
    for table in ("instrument_symbol_history", "watchlist"):
        for right in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert conn.execute(
                "SELECT has_table_privilege(current_user, %s, %s)", (table, right)
            ).fetchone()[0], f"mi_app lacks {right} on {table}"
    # The identity sequence works for mi_app (default privileges from 0002).
    inst_id = conn.execute(
        "INSERT INTO instruments (segment, symbol, is_active) VALUES ('EQ', 'ZZSEQ', true)"
        " RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO instrument_symbol_history (instrument_id, old_symbol, new_symbol, changed_on)"
        " VALUES (%s, 'A', 'B', %s)",
        (inst_id, DAY1),
    )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO watchlist (instrument_id, note) VALUES (%s, %s)", (inst_id, "x" * 201)
        )


def test_refresh_then_a_second_refresh_changes_nothing(conn):
    plan = refresh(conn, FILE)
    assert len(plan.inserts) == 3 and plan.changes == 3
    _, series, active, first, last = row(conn, "ZZFAKEB")
    assert (series, active, first, last) == ("BE", True, DAY1, DAY1)
    index_row = conn.execute(
        "SELECT segment, series, isin FROM instruments WHERE symbol = 'ZZ FAKE 50'"
    ).fetchone()
    assert index_row == ("INDEX", None, None)
    updated_at = conn.execute(
        "SELECT max(updated_at) FROM instruments WHERE symbol LIKE 'ZZ%'"
    ).fetchone()[0]
    again = refresh(conn, FILE)
    assert again.changes == 0 and not again.seen
    assert (
        conn.execute("SELECT max(updated_at) FROM instruments WHERE symbol LIKE 'ZZ%'").fetchone()[
            0
        ]
        == updated_at
    )
    later = refresh(conn, FILE, today=DAY2)
    assert later.changes == 0 and row(conn, "ZZFAKEA")[4] == DAY2  # only last_seen_on moved


def test_deactivate_reactivate_and_symbol_history(conn):
    refresh(conn, FILE)
    first_id = row(conn, "ZZFAKEA")[0]
    gone = refresh(conn, FILE[1:] + [fake("ZZFAKEC", 3)])  # 3 kept of 3 active: allowed
    assert len(gone.deactivate) == 1 and row(conn, "ZZFAKEA")[2] is False
    back = refresh(conn, FILE)
    assert back.reactivated == 1 and row(conn, "ZZFAKEA")[:3] == (first_id, "EQ", True)
    renamed = [FILE[0].model_copy(update={"symbol": "ZZFAKEA2"}), *FILE[1:]]
    refresh(conn, renamed, today=DAY2)
    history = conn.execute(
        "SELECT old_symbol, new_symbol, changed_on FROM instrument_symbol_history"
        " WHERE instrument_id = %s",
        (first_id,),
    ).fetchall()
    assert history == [("ZZFAKEA", "ZZFAKEA2", DAY2)]


def test_two_instruments_swap_symbols(conn):
    refresh(conn, FILE)
    swapped = [
        FILE[0].model_copy(update={"symbol": "ZZFAKEB"}),
        FILE[1].model_copy(update={"symbol": "ZZFAKEA"}),
        FILE[2],
    ]
    plan = refresh(conn, swapped)
    assert len(plan.history) == 2
    assert row(conn, "ZZFAKEA")[1] == "BE" and row(conn, "ZZFAKEB")[1] == "EQ"


def test_a_symbol_is_reused_after_the_old_instrument_goes_inactive(conn):
    refresh(conn, FILE)
    refresh(conn, FILE[1:] + [fake("ZZFAKEC", 3)])  # ZZFAKEA (old ISIN) inactive
    newcomer = fake("ZZFAKEA", 9)  # a different company takes the symbol
    plan = refresh(conn, [newcomer, *FILE[1:], fake("ZZFAKEC", 3)])
    assert len(plan.inserts) == 1
    rows = conn.execute(
        "SELECT isin, is_active FROM instruments WHERE symbol = 'ZZFAKEA' ORDER BY id"
    ).fetchall()
    assert rows == [(FILE[0].isin, False), (newcomer.isin, True)]
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(  # two ACTIVE rows with one symbol are still impossible
            "INSERT INTO instruments (segment, symbol, is_active) VALUES ('EQ', 'ZZFAKEA', true)"
        )


def test_watchlist_add_list_remove(conn):
    refresh(conn, FILE)
    assert watchlist.add(conn, "zzfakeb", "fake note")[-1].startswith("note: series BE")
    assert watchlist.add(conn, "ZZFAKEA") == ["added ZZFAKEA (2 of 50)"]
    with pytest.raises(watchlist.WatchlistError, match="is an index"):
        watchlist.add(conn, "ZZ FAKE 50")
    lines = watchlist.list_lines(conn, ZoneInfo(get_settings().display_timezone))
    assert lines[0] == "watchlist: 2 of 50"
    assert lines[2].startswith("ZZFAKEA") and lines[3].startswith("ZZFAKEB")
    assert watchlist.remove(conn, "ZZFAKEA") == ["removed ZZFAKEA"]
    assert watchlist.remove(conn, "ZZFAKEB") == ["removed ZZFAKEB"]
    assert conn.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 0


def test_watchlist_refuses_the_51st(conn):
    items = [fake(f"ZZW{i:02d}", 100 + i) for i in range(51)]
    refresh(conn, items)
    for i in range(50):
        watchlist.add(conn, f"ZZW{i:02d}")
    with pytest.raises(watchlist.WatchlistError, match="full"):
        watchlist.add(conn, "ZZW50")
    assert conn.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 50
