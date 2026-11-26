"""S6a as the app login: migration 0011 (mf_schemes, mf_nav, mf_nav_chunks), the scheme master,
a NAV load from a FAKE AMFI file, the resumable-rerun rule and the stats engine reading back
out of the database. One transaction, rolled back. Runs in CI compose-smoke (RUN_INTEGRATION=1).

The fake file uses scheme codes in a range AMFI does not issue (9xxxxxx), so nothing here can
collide with real data even if the rollback were to fail.
"""

from collections.abc import Iterator
from datetime import date, timedelta

import psycopg
import pytest

from market_intel import mf, mf_fetch
from market_intel.config import get_settings
from market_intel.db import postgres

pytestmark = pytest.mark.integration

EQUITY = 9100001
INDEX = 9100002
DEBT = 9100003  # outside the universe: its NAV must NOT be stored


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            yield c


def fake_master() -> str:
    return (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;"
        "Option;Net Asset Value;Date\n"
        "\n"
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n"
        "\n"
        "ZZ Fake Mutual Fund\n"
        "\n"
        f"{EQUITY};INFZZZ000001;-;ZZ Fake Large Cap Fund;Direct Plan;Growth;100.0000;01-Mar-2024\n"
        "\n"
        "Open Ended Schemes(Index Funds - Equity Funds)\n"
        "\n"
        "ZZ Fake Mutual Fund\n"
        "\n"
        f"{INDEX};INFZZZ000002;-;ZZ Fake Nifty 50 Index Fund;Direct Plan;Growth;50.0000;"
        "01-Mar-2024\n"
        "\n"
        "Open Ended Schemes(Debt Scheme - Liquid Fund)\n"
        "\n"
        "ZZ Fake Mutual Fund\n"
        "\n"
        f"{DEBT};INFZZZ000003;-;ZZ Fake Liquid Fund;Direct Plan;Growth;10.0000;01-Mar-2024\n"
    )


def fake_history(start: date, days: int) -> str:
    """The HISTORY layout, whose columns are ordered differently from the daily file."""
    head = (
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
        "Net Asset Value;Date\n\nOpen Ended Schemes ( Equity Scheme - Large Cap Fund )\n\n"
        "ZZ Fake Mutual Fund\n\n"
    )
    rows = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%d-%b-%Y")
        rows.append(
            f"{EQUITY};ZZ Fake Large Cap Fund - Direct - Growth;Direct Plan;Growth;"
            f"INFZZZ000001;-;{100.0 + i:.4f};{d}"
        )
        rows.append(
            f"{INDEX};ZZ Fake Nifty 50 Index Fund - Direct - Growth;Direct Plan;Growth;"
            f"INFZZZ000002;-;{50.0 + i * 0.5:.4f};{d}"
        )
        rows.append(
            f"{DEBT};ZZ Fake Liquid Fund - Direct - Growth;Direct Plan;Growth;"
            f"INFZZZ000003;-;{10.0 + i * 0.01:.4f};{d}"
        )
    return head + "\n".join(rows) + "\n"


def test_the_three_tables_exist_and_the_app_login_can_use_them(conn):
    for table in ("mf_schemes", "mf_nav", "mf_nav_chunks"):
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()


def test_the_master_stores_every_scheme_but_flags_only_the_universe(conn, tmp_path):
    path = tmp_path / "NAVAll.txt"
    path.write_text(fake_master(), encoding="utf-8")
    result = mf_fetch.store_schemes(conn, mf_fetch.read_schemes(path), seen=date(2024, 3, 1))

    assert result.schemes == 3  # the debt fund is STORED...
    assert result.universe == 2  # ...but not in the universe
    assert result.by_category == {"equity": 1, "index": 1}

    rows = dict(
        conn.execute(
            "SELECT scheme_code, in_universe FROM mf_schemes WHERE scheme_code = ANY(%s)",
            ([EQUITY, INDEX, DEBT],),
        ).fetchall()
    )
    assert rows == {EQUITY: True, INDEX: True, DEBT: False}


def test_a_nav_load_stores_the_universe_only_and_a_rerun_writes_nothing(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))
    codes = mf_fetch.universe_codes(conn)
    assert DEBT not in codes

    history = tmp_path / "chunk.txt"
    history.write_text(fake_history(date(2024, 3, 1), 10), encoding="utf-8")

    written = mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, codes))
    assert written == 20  # 10 days x 2 universe schemes; the debt fund's 10 are not stored

    held = conn.execute("SELECT count(*) FROM mf_nav WHERE scheme_code = %s", (DEBT,)).fetchone()[0]
    assert held == 0

    # The resumable rule: loading the same chunk again writes NOTHING.
    again = mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, codes))
    assert again == 0


def test_a_loaded_chunk_is_recorded_and_then_skipped(conn):
    a, b = date(2024, 1, 1), date(2024, 3, 30)
    assert (a, b) not in mf_fetch.loaded_chunks(conn)
    mf_fetch.record_chunk(conn, a, b, rows=20, size=1234, sha256="0" * 64)
    assert (a, b) in mf_fetch.loaded_chunks(conn)


def test_scheme_dates_are_refreshed_from_the_nav_actually_held(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))
    history = tmp_path / "chunk.txt"
    history.write_text(fake_history(date(2024, 3, 1), 10), encoding="utf-8")
    mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, mf_fetch.universe_codes(conn)))

    mf_fetch.refresh_scheme_dates(conn)
    first, last = conn.execute(
        "SELECT first_seen, last_seen FROM mf_schemes WHERE scheme_code = %s", (EQUITY,)
    ).fetchone()
    assert (first, last) == (date(2024, 3, 1), date(2024, 3, 10))


def test_the_stats_engine_reads_its_series_back_out_of_the_database(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))
    history = tmp_path / "chunk.txt"
    history.write_text(fake_history(date(2024, 3, 1), 40), encoding="utf-8")
    mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, mf_fetch.universe_codes(conn)))

    series = mf_fetch.load_series(conn, [EQUITY, INDEX, DEBT])
    assert set(series) == {EQUITY, INDEX}  # the debt fund has no stored NAV at all
    assert len(series[EQUITY]) == 40
    assert series[EQUITY] == sorted(series[EQUITY])  # ascending, as the stats engine needs

    names = mf_fetch.scheme_rows(conn, [EQUITY])
    stats = mf.fund_stats(EQUITY, *names[EQUITY], series[EQUITY], years=(1,), levels=(0.10,))
    assert stats.observations == 40
    assert stats.start == date(2024, 3, 1) and stats.end == date(2024, 4, 9)
    assert stats.max_drawdown.depth == 0.0  # the fake NAV only rises
    assert stats.rolling[1].count == 0  # 40 days is not a year
    assert stats.recovery[0.10].episodes == 0
