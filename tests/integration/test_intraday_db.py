"""S4i as the app login: 1-minute candles (upsert, rerun, thin days), the feature and label
builds (Parquet + feature_builds) and the leakage test on FAKE minute bars, all in ONE
transaction that is rolled back (Parquet goes to a temp folder). Safe on a real local database.
Runs in CI compose-smoke (RUN_INTEGRATION=1)."""

from collections.abc import Iterator
from datetime import date

import pandas as pd
import psycopg
import pytest
from intraday_fakes import business_days, daily_from, minute_bars

from market_intel import intraday as it
from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.intraday_candles import IntradayStore

pytestmark = pytest.mark.integration

UNIVERSE = "zz_intraday_test"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            yield c


def as_rows(bars: pd.DataFrame) -> pd.DataFrame:
    out = bars.reset_index()[["ts", "open", "high", "low", "close", "volume"]].copy()
    out[["open", "high", "low", "close"]] = out[["open", "high", "low", "close"]].round(4)
    out["high"] = out[["open", "high", "close"]].max(axis=1)
    out["low"] = out[["open", "low", "close"]].min(axis=1)
    out["volume"] = out["volume"].astype("int64")
    out["oi"] = 0
    return out


@pytest.fixture
def fake(conn):
    days = business_days(date(2021, 6, 1), 45)  # before any real 1-minute data (2022+)
    store, ids = IntradayStore(conn), []
    for i in range(3):
        inst = conn.execute(
            "INSERT INTO instruments (segment, symbol, instrument_key, is_active)"
            " VALUES ('EQ', %s, %s, true) RETURNING id",
            (f"ZZI{i}", f"NSE_EQ|ZZINTRA{i}"),
        ).fetchone()[0]
        bars = minute_bars(days, 100 + i)
        if i == 2:  # a thin day: only 200 bars on days[30]
            local = bars.index.tz_convert("Asia/Kolkata")
            late = local.hour * 60 + local.minute >= 9 * 60 + 15 + 200
            bars = bars[~((local.date == days[30]) & late)]
        rows = as_rows(bars)
        assert store.upsert(inst, rows) == len(rows)
        assert store.upsert(inst, rows) == 0  # a rerun writes nothing
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO candles_daily (instrument_id, trade_date, open, high, low, close,"
                " volume, price_basis, source)"
                " VALUES (%s, %s, %s, %s, %s, %s, 0, 'raw', 'upstox')",
                [
                    (inst, d, *(round(float(x), 4) for x in r))
                    for d, r in daily_from(bars).iterrows()
                ],
            )  # fmt: skip
        conn.execute(
            "INSERT INTO universe_members (universe, instrument_id, as_of) VALUES (%s, %s, %s)",
            (UNIVERSE, inst, days[-1]),
        )
        ids.append(inst)
    return days, ids


def test_minute_candles_upsert_and_thin_days_as_the_app_login(conn, fake):
    days, ids = fake
    store = IntradayStore(conn)
    assert store.last_day(ids[0]) == days[-1]
    store.save_thin_days(ids[2], {days[30]: 200})
    store.save_thin_days(ids[2], {days[30]: 200})  # idempotent
    assert store.thin_day_count(ids) == 1
    note = conn.execute(
        "SELECT interval, note FROM data_gaps WHERE instrument_id = %s", (ids[2],)
    ).fetchone()
    assert note == ("1m", "thin day: 200 bars")


def test_features_labels_and_leakage_on_fake_minutes(conn, fake, tmp_path):
    days, ids = fake
    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    feats = it.build(settings, conn, "intraday_features", universe=UNIVERSE, gate_samples=5)
    assert feats.rows == 3 * 45 * 67 - (67 - 37) and feats.files == 3  # June, July, August 2021
    frame = pd.read_parquet(tmp_path / "features/intraday/v1/year=2021/month=07")
    assert list(frame.columns[:5]) == ["instrument_id", "version", "ts", "trade_date", "minute"]
    labels = it.build(settings, conn, "intraday_labels", universe=UNIVERSE, gate_samples=0)
    assert labels.excluded.get("thin_day") == 37  # 09:30..14:30 have a bar
    assert set(labels.label_mix) == {"long", "short"}
    recorded = conn.execute(
        "SELECT kind, rows, files, jsonb_typeof(sha256) FROM feature_builds"
        " WHERE from_date = %s ORDER BY kind",
        (days[0],),
    ).fetchall()
    assert recorded == [
        ("intraday_features", feats.rows, 3, "object"),
        ("intraday_labels", labels.rows, 3, "object"),
    ]
    report, n = it.check_real(conn, 20, universe=UNIVERSE)
    assert report.passed, report.lines()
    assert n == 20
