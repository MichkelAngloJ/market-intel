"""S2 as the app login: migration 0007 (features_daily, labels_daily), special-session detection,
a feature build and labels on FAKE data (NULL around a price event), all in ONE transaction that
is rolled back. Safe on a real local database. Runs in CI compose-smoke (RUN_INTEGRATION=1)."""

from collections.abc import Iterator
from datetime import date, timedelta

import psycopg
import pytest
from feature_fakes import business_days, candles

from market_intel import features as F
from market_intel.candles import special_sessions
from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.feature_spec import FEATURE_NAMES

pytestmark = pytest.mark.integration

CAL_KEY, VIX_KEY = "NSE_INDEX|ZZ Fake 50", "NSE_INDEX|ZZ Fake VIX"
UNIVERSE = "zz_test_universe"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            yield c


def add(conn, symbol, segment, key=None, sector=None):
    return conn.execute(
        "INSERT INTO instruments (segment, symbol, instrument_key, sector, is_active)"
        " VALUES (%s, %s, %s, %s, false) RETURNING id",
        (segment, symbol, key, sector),
    ).fetchone()[0]


def put(conn, inst, frame):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO candles_daily (instrument_id, trade_date, open, high, low, close, volume,"
            " price_basis, source) VALUES (%s, %s, %s, %s, %s, %s, %s, 'raw', 'upstox')",
            [
                (inst, d, *(round(getattr(r, k), 4) for k in ("open", "high", "low", "close")),
                 int(r.volume))
                for d, r in frame.iterrows()
            ],
        )  # fmt: skip


@pytest.fixture
def fake(conn):
    days = business_days(date(2025, 1, 6), 280)
    special = days[100] + timedelta(days=5 - days[100].weekday())  # a Saturday
    cal = add(conn, "ZZCAL", "INDEX", CAL_KEY)
    vix = add(conn, "ZZVIX", "INDEX", VIX_KEY)
    put(conn, cal, candles(days + [special], 50, 20000.0).sort_index())
    put(conn, vix, candles(days, 51, 15.0))
    stocks = []
    for i in range(4):
        inst = add(conn, f"ZZF{i}", "EQ", sector="Zz Sector" if i < 2 else "Zz Other")
        frame = candles(days, 60 + i)
        if i == 0:
            frame = frame.drop(index=[days[50]])  # a gap
        if i == 3:
            put(conn, inst, candles([special], 70))  # one stock trades the special session
        put(conn, inst, frame)
        conn.execute(
            "INSERT INTO universe_members (universe, instrument_id, as_of) VALUES (%s, %s, %s)",
            (UNIVERSE, inst, days[-1]),
        )
        stocks.append(inst)
    conn.execute(
        "INSERT INTO price_events (instrument_id, trade_date, kind, ratio)"
        " VALUES (%s, %s, 'suspected_split_bonus', 0.5)",
        (stocks[1], days[200]),
    )
    return days, special, stocks


def test_0007_tables_as_the_app_login(conn):
    assert conn.execute("SELECT current_user").fetchone()[0] == "mi_app"
    for table in ("features_daily", "labels_daily"):
        for right in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert conn.execute(
                "SELECT has_table_privilege(current_user, %s, %s)", (table, right)
            ).fetchone()[0], f"mi_app lacks {right} on {table}"
    inst = add(conn, "ZZCHK", "EQ")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(  # a label AND a reason at once is refused
            "INSERT INTO labels_daily (instrument_id, trade_date, horizon_days, fwd_return, up,"
            " excluded_reason) VALUES (%s, '2025-01-06', 21, 0.1, true, 'gap')",
            (inst,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO labels_daily (instrument_id, trade_date, horizon_days, excluded_reason)"
            " VALUES (%s, '2025-01-06', 5, 'gap')",
            (inst,),
        )


def test_special_sessions_and_a_build_on_fake_data(conn, fake):
    days, special, stocks = fake
    assert special_sessions(conn, universe=UNIVERSE, calendar_key=CAL_KEY) == [special]
    inputs = F.load_inputs(
        conn, universe=UNIVERSE, nifty_key=CAL_KEY, vix_key=VIX_KEY, with_news=False
    )
    assert special not in inputs.calendar and all(
        special not in f.index for f in inputs.candles.values()
    )
    summary = F.build_and_store(get_settings(), conn, universe=UNIVERSE, inputs=inputs)
    assert summary.rows == sum(len(f) for f in inputs.candles.values())
    again = F.build_and_store(get_settings(), conn, universe=UNIVERSE, inputs=inputs)
    assert again.rows == 0  # incremental
    stored = conn.execute(
        "SELECT ret_20d, history_days FROM features_daily WHERE instrument_id = %s"
        " AND trade_date = %s AND feature_version = 'v1'",
        (stocks[2], days[100]),
    ).fetchone()
    expected = F.features_for(inputs, stocks[2]).loc[days[100]]
    assert stored == pytest.approx((expected["ret_20d"], expected["history_days"]))
    columns = [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'features_daily'"
        ).fetchall()
    ]
    assert set(FEATURE_NAMES) <= set(columns) and "fwd_return" not in columns


def test_labels_are_null_around_a_price_event_and_a_gap(conn, fake):
    days, special, stocks = fake
    inputs = F.load_inputs(
        conn, universe=UNIVERSE, nifty_key=CAL_KEY, vix_key=VIX_KEY, with_news=False
    )
    result = F.labels_and_store(conn, inputs=inputs)
    assert result.rows == 3 * sum(len(f) for f in inputs.candles.values())
    reason = lambda inst, d, h: conn.execute(  # noqa: E731
        "SELECT excluded_reason, fwd_return FROM labels_daily WHERE instrument_id = %s"
        " AND trade_date = %s AND horizon_days = %s",
        (inst, d, h),
    ).fetchone()
    assert reason(stocks[1], days[190], 21) == ("price_event", None)
    assert reason(stocks[0], days[40], 21) == ("gap", None)
    assert reason(stocks[2], days[90], 21)[0] == "special_session"
    assert reason(stocks[2], days[-5], 21) == ("beyond_data", None)
    ok = reason(stocks[2], days[20], 21)
    assert ok[0] is None and ok[1] is not None
