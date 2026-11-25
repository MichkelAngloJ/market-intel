"""The intraday commands end to end (S5i): a small Parquet store in a temp folder, a stub
connection, and the backtest / report / replay output. No database, no candles."""

from __future__ import annotations

from datetime import date

import intraday_fakes as fakes
import pandas as pd
import pytest

from market_intel import intraday as I
from market_intel import intraday_run as R
from market_intel.config import ConfigError, Settings


class StubConn:
    """Answers the few reads the commands make and records what they would have written."""

    def __init__(self, symbols: dict[int, str] | None = None):
        self.symbols = symbols or {1: "ZZA", 2: "ZZB", 3: "ZZC"}
        self.inserts: list[str] = []
        self.rows: list[tuple] = []

    def execute(self, sql, params=None):
        self.inserts.append(sql)
        self._last = (sql, params)
        return self

    def fetchone(self):
        sql = self._last[0]
        if "RETURNING id" in sql:
            return (len(self.inserts),)
        if "coalesce(max(version)" in sql:
            return (1,)
        return (0,)

    def fetchall(self):
        sql = self._last[0]
        if "universe_members" in sql:
            return list(self.symbols.items())
        return self.rows

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        self.inserts.append(f"{sql.split()[2]} x{len(rows)}")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build features and both label versions from synthetic bars into a temp DATA_DIR."""
    data_dir = tmp_path_factory.mktemp("data")
    # built explicitly: this fixture is module-scoped, so it runs before the autouse
    # environment isolation and must not depend on any .env file
    settings = Settings(_env_file=None, app_profile="restricted", data_dir=str(data_dir))
    inputs = fakes.inputs(40)
    features, v1, v2 = [], [], []
    for inst in inputs.bars:
        f = I.decisions(I.build_minute_features(inputs, inst)).reset_index(names="ts")
        f.insert(0, "instrument_id", inst)
        features.append(f)
        a = I.build_labels(inputs, inst)
        a.insert(0, "instrument_id", inst)
        v1.append(a)
        b = I.build_labels_volatility(inputs, inst)
        b.insert(0, "instrument_id", inst)
        v2.append(b)
    for kind, version, parts in (
        ("intraday_features", I.VERSION, features),
        ("intraday_labels", "v1", v1),
        ("intraday_labels", "v2", v2),
    ):
        frame = pd.concat(parts, ignore_index=True)
        for month, chunk in frame.groupby(
            [pd.to_datetime(frame["trade_date"]).dt.to_period("M")], sort=True
        ):
            stamp = month[0].to_timestamp().date() if isinstance(month, tuple) else month
            folder = I.month_dir(settings, kind, version, stamp)
            folder.mkdir(parents=True, exist_ok=True)
            chunk.to_parquet(folder / "part-0.parquet", index=False)
    return settings


def test_the_built_store_reads_back(built):
    features = I.load_built(built, "intraday_features", I.VERSION)
    assert not features.empty
    assert set(I.FEATURES_V1) <= set(features.columns)
    assert isinstance(features["trade_date"].iloc[0], date)
    labels = I.load_built(built, "intraday_labels", "v2")
    assert f"outcome_{I.VARIANTS[0].name}" in labels.columns


def test_a_month_range_limits_what_is_read(built):
    everything = I.load_built(built, "intraday_features", I.VERSION)
    first_day = min(everything["trade_date"])
    narrow = I.load_built(built, "intraday_features", I.VERSION, start=first_day, end=first_day)
    assert set(narrow["trade_date"]) == {first_day}


def test_reading_a_version_that_was_never_built_is_an_error(built):
    with pytest.raises(ConfigError, match="nothing built yet"):
        I.load_built(built, "intraday_labels", "v9")


def test_the_backtest_compares_every_rule_with_no_trading(built):
    conn = StubConn()
    report = R.run_backtest(
        built, conn, capital=100_000, label_version="v2", variant="k15_b10",
        slippage_grid=(0.0, 5.0), headline_slippage=5.0, save=False,
    )  # fmt: skip
    names = {r.name for r in report.results}
    assert "no_trading" in names
    assert {"orb_15", "vwap_trend", "gap_fill"} <= names
    text = "\n".join(report.lines())
    assert "net Rs" in text and "slippage per side" in text
    assert "charges alone cost" in text
    # the sensitivity grid has one line per slippage level
    assert set(report.sensitivity) == {0.0, 5.0}
    assert report.sensitivity[0.0]["net_profit"] >= report.sensitivity[5.0]["net_profit"]


def test_the_backtest_can_store_its_runs(built):
    conn = StubConn()
    R.run_backtest(
        built, conn, label_version="v2", variant="k15_b10",
        slippage_grid=(5.0,), headline_slippage=5.0, save=True,
    )  # fmt: skip
    assert any("INSERT INTO backtest_runs" in sql for sql in conn.inserts)


def test_v1_labels_work_too(built):
    report = R.run_backtest(
        built, StubConn(), label_version="v1", slippage_grid=(5.0,), headline_slippage=5.0,
        save=False,
    )  # fmt: skip
    assert all(r.label_version in ("v1", "-") for r in report.results)


def test_the_report_reads_the_stored_runs():
    conn = StubConn()
    conn.rows = [
        ("strategy", "orb_15", "v2", "k15_b10", "walk_forward", date(2024, 3, 1),
         date(2024, 4, 30), 12, 0.25, -1500.0, -1.5, 2.0, -0.5, 40, 10, 5.0),
    ]  # fmt: skip
    lines = R.run_report(conn)
    assert lines[0].startswith("kind")
    assert "orb_15" in lines[-1] and "-1,500" in lines[-1]


def test_an_empty_report_says_so():
    assert "no backtests stored yet" in R.run_report(StubConn())[0]


def test_the_replay_defaults_to_the_last_built_day(built):
    last = R.last_built_day(built, "v2")
    features = I.load_built(built, "intraday_features", I.VERSION, columns=["trade_date"])
    assert last == max(features["trade_date"])


def test_the_replay_prints_plans_and_the_day_total(built):
    lines = R.run_replay(
        built, StubConn(), capital=100_000, label_version="v2", variant="k15_b10",
        strategy="vwap_reversion",
    )  # fmt: skip
    text = "\n".join(lines)
    assert text.startswith("replay ")
    assert "risk 1% per trade" in text
    assert "day total" in text or "no signals" in text
    if "day total" in text:
        assert "P(target)" in text and "flat by 15:20" in text
        assert "costs Rs" in text


def test_the_replay_refuses_an_unknown_strategy(built):
    with pytest.raises(ConfigError, match="unknown strategy"):
        R.run_replay(built, StubConn(), strategy="moon_phase")


def test_the_replay_refuses_a_day_that_was_never_built(built):
    with pytest.raises(ConfigError):
        R.run_replay(built, StubConn(), day=date(2019, 1, 2))


def test_training_needs_built_rows(built, tmp_path):
    empty = built.model_copy(update={"data_dir": tmp_path})
    with pytest.raises(ConfigError):
        R.run_train(empty, StubConn(), save=False)
