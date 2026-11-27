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


def fake_history_with_a_fall(start: date, days: int) -> str:
    """Six years of rising NAV with one deliberate 30% fall and recovery in the middle.

    The S6b commands need enough history to pass the five-year rule and something real to
    find: a straight line would make every recovery statistic empty.
    """
    head = (
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
        "Net Asset Value;Date\n\nOpen Ended Schemes ( Equity Scheme - Large Cap Fund )\n\n"
        "ZZ Fake Mutual Fund\n\n"
    )

    def nav(i: int, base: float) -> float:
        level = base * (1.0 + 0.0004) ** i
        if 600 <= i < 700:  # the fall
            level *= 1.0 - 0.30 * (i - 600) / 100
        elif 700 <= i < 1000:  # the climb back
            level *= 0.70 + 0.30 * (i - 700) / 300
        return level

    rows = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%d-%b-%Y")
        rows.append(
            f"{EQUITY};ZZ Fake Large Cap Fund - Direct - Growth;Direct Plan;Growth;"
            f"INFZZZ000001;-;{nav(i, 100.0):.4f};{d}"
        )
        rows.append(
            f"{INDEX};ZZ Fake Nifty 50 Index Fund - Direct - Growth;Direct Plan;Growth;"
            f"INFZZZ000002;-;{nav(i, 50.0):.4f};{d}"
        )
    return head + "\n".join(rows) + "\n"


def load_fake(conn, tmp_path, days: int, start: date = date(2019, 1, 1)) -> None:
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=start)
    history = tmp_path / "chunk.txt"
    history.write_text(fake_history_with_a_fall(start, days), encoding="utf-8")
    mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, mf_fetch.universe_codes(conn)))


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


def test_streaming_one_fund_at_a_time_gives_the_same_series_as_loading_them_all(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))
    history = tmp_path / "chunk.txt"
    history.write_text(fake_history(date(2024, 3, 1), 30), encoding="utf-8")
    mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history, mf_fetch.universe_codes(conn)))

    whole = mf_fetch.load_series(conn, [EQUITY, INDEX])
    streamed = dict(mf_fetch.iter_series(conn, [EQUITY, INDEX]))
    assert streamed == whole
    assert len(streamed) == 2


# --- the S6b decision commands, against the database ---------------------------------------
def test_scheme_details_carry_the_fund_house_a_sip_plan_needs(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))

    details = mf_fetch.scheme_details(conn, [EQUITY, INDEX, DEBT])
    assert set(details) == {EQUITY, INDEX}  # the debt fund is not in the universe
    assert details[EQUITY].amc == "ZZ Fake Mutual Fund"
    assert details[EQUITY].category == "equity"
    assert details[INDEX].category == "index"


def test_the_minimum_history_rule_is_applied_to_what_the_database_actually_holds(conn, tmp_path):
    load_fake(conn, tmp_path, days=700)  # under two years: too new to judge
    stats = [
        mf.fund_stats(code, f"fund {code}", "equity", rows)
        for code, rows in mf_fetch.iter_series(conn, [EQUITY, INDEX])
    ]
    assert len(stats) == 2
    assert all(mf.history_grade(s.years) == mf.TOO_NEW for s in stats)
    keep, dropped = mf.eligible(stats, mf.RISK_BANDS["high"])
    assert keep == []
    assert len(dropped) == 2


def test_the_whole_decision_pipeline_runs_on_a_series_read_back_from_the_database(conn, tmp_path):
    load_fake(conn, tmp_path, days=2_600)  # a bit over seven years
    details = mf_fetch.scheme_details(conn)
    stats = [
        mf.fund_stats(code, details[code].scheme_name, details[code].category, rows)
        for code, rows in mf_fetch.iter_series(conn, [EQUITY, INDEX])
    ]
    keep, _dropped = mf.eligible(stats, mf.RISK_BANDS["high"])
    assert {s.scheme_code for s in keep} == {EQUITY, INDEX}
    assert all(s.grade == mf.RANKED for s in keep)

    ranked = mf.rank_lumpsum(keep)
    assert len(ranked) == 2
    # The same funds, handed over in the other order, rank the same way.
    assert [r.stats.scheme_code for r in ranked] == [
        r.stats.scheme_code for r in mf.rank_lumpsum(list(reversed(keep)))
    ]

    # Both fake funds belong to one house, so the AMC rule is what limits the plan to one.
    amcs = {code: info.amc for code, info in details.items()}
    assert len(mf.diversify(ranked, 2, amcs)) == 1
    picked = mf.diversify(ranked, 2, {})
    assert len(picked) == 2

    plan = mf.split_sip(picked, 10_000)
    assert sum(s.monthly for s in plan.slices) == 10_000

    series = {code: mf.Series.build(rows) for code, rows in mf_fetch.iter_series(conn)}
    outcome = mf.sip_windows([(s.monthly, series[s.scheme_code]) for s in plan.slices], 5)
    assert outcome.windows > 12
    assert outcome.invested == 600_000  # 10,000 a month for sixty months
    assert outcome.multiple.worst is not None and outcome.multiple.worst > 0
    assert outcome.annualised.count == outcome.windows


def test_keep_or_exit_finds_the_fall_that_was_written_into_the_fake_nav(conn, tmp_path):
    load_fake(conn, tmp_path, days=2_600)
    series = mf.Series.build(dict(mf_fetch.iter_series(conn, [EQUITY]))[EQUITY])
    episodes = mf.fall_episodes(series, 0.20)

    assert len(episodes) == 1  # one long slide counts once, not once a day
    (episode,) = episodes
    # The written-in cut is 30%, but the underlying trend keeps rising through it, so the
    # fall measured from the peak is 27.1%. That gap is the point: the depth is read off the
    # NAV, not off the number that generated it.
    assert episode.depth == pytest.approx(0.271, abs=0.005)
    assert episode.recovered_on is not None

    evidence = mf.recovery_evidence(episodes, 0.20, horizon_days=365 * 3)
    assert evidence.recovered == 1
    assert evidence.still_down == 0
    assert evidence.within_horizon + evidence.beyond_horizon == 1


def test_only_one_plan_is_ranked_so_a_fund_cannot_fill_a_table_twice(conn, tmp_path):
    master = tmp_path / "NAVAll.txt"
    master.write_text(fake_master(), encoding="utf-8")
    mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master), seen=date(2024, 3, 1))

    direct = mf_fetch.scheme_details(conn, [EQUITY, INDEX], plan="direct")
    regular = mf_fetch.scheme_details(conn, [EQUITY, INDEX], plan="regular")
    assert set(direct) == {EQUITY, INDEX}  # the fake master is Direct Plan throughout
    assert regular == {}
    assert direct[EQUITY].plan == "direct"
