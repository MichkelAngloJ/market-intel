"""S5i as the app login: migration 0010 (backtest_runs, backtest_trades, the intraday model
kind), storing a backtest with its trades, the FK cascade and the check constraints. One
transaction, rolled back. Safe on a real local database; runs in CI compose-smoke."""

from collections.abc import Iterator
from datetime import UTC, date, datetime

import psycopg
import pytest

from market_intel import intraday_backtest as B
from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.sizing import SizingPolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            yield c


@pytest.fixture
def instrument(conn) -> int:
    return conn.execute(
        "INSERT INTO instruments (segment, symbol, instrument_key, is_active)"
        " VALUES ('EQ', 'ZZBT0', 'NSE_EQ|ZZBT0', true) RETURNING id"
    ).fetchone()[0]


def result_with_trades(instrument: int) -> B.BacktestResult:
    day = date(2024, 3, 5)
    result = B.BacktestResult(
        name="orb_15", kind="strategy", label_version="v2", variant="k15_b10",
        period="walk_forward", capital=100_000.0, slippage_bps=5.0,
        from_date=day, to_date=day, days=1, params={"rule": "test"},
    )  # fmt: skip
    result.trades.append(
        B.Trade(
            instrument_id=instrument,
            trade_date=day,
            decided_at=datetime(2024, 3, 5, 4, 0, tzinfo=UTC),
            side="long",
            entry=100.0,
            exit_price=100.5,
            target=100.5,
            stop=99.75,
            quantity=1_000,
            bars_held=4,
            exit_reason="target",
            gross_profit=500.0,
            costs=120.0,
            net_profit=380.0,
            probability=0.42,
        )  # fmt: skip
    )
    result.daily_profit[day] = 380.0
    return result


def test_the_app_login_can_store_and_read_a_backtest(conn, instrument):
    for table in ("backtest_runs", "backtest_trades"):
        for right in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert conn.execute(
                "SELECT has_table_privilege(current_user, %s, %s)", (table, right)
            ).fetchone()[0], f"{right} on {table}"
    run_id = B.save(conn, result_with_trades(instrument))
    stored = conn.execute(
        "SELECT kind, name, label_version, variant, period, trades, net_profit, costs_paid,"
        " days_without_trade, jsonb_typeof(params) FROM backtest_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert stored == (
        "strategy", "orb_15", "v2", "k15_b10", "walk_forward", 1, 380.00, 120.00, 0, "object",
    )  # fmt: skip
    trade = conn.execute(
        "SELECT side, entry, exit_price, quantity, bars_held, exit_reason, net_profit,"
        " probability FROM backtest_trades WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    assert trade[:7] == ("long", 100.0, 100.5, 1_000, 4, "target", 380.00)
    assert trade[7] == pytest.approx(0.42)


def test_deleting_a_run_takes_its_trades_with_it(conn, instrument):
    run_id = B.save(conn, result_with_trades(instrument))
    conn.execute("DELETE FROM backtest_runs WHERE id = %s", (run_id,))
    left = conn.execute(
        "SELECT count(*) FROM backtest_trades WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    assert left == 0


def test_the_check_constraints_refuse_nonsense(conn, instrument):
    run_id = B.save(conn, result_with_trades(instrument), with_trades=False)
    bad_rows = [
        ("kind", "INSERT INTO backtest_runs (kind, name, label_version, period, from_date,"
                 " to_date, capital, slippage_bps, trades, net_profit, costs_paid, return_pct,"
                 " max_drawdown_pct, days, days_without_trade, params)"
                 " VALUES ('guess', 'x', 'v1', 'walk_forward', '2024-03-05', '2024-03-05',"
                 " 100000, 5, 0, 0, 0, 0, 0, 1, 1, '{}')"),
        ("capital", "INSERT INTO backtest_runs (kind, name, label_version, period, from_date,"
                    " to_date, capital, slippage_bps, trades, net_profit, costs_paid,"
                    " return_pct, max_drawdown_pct, days, days_without_trade, params)"
                    " VALUES ('strategy', 'x', 'v1', 'walk_forward', '2024-03-05',"
                    " '2024-03-05', 0, 5, 0, 0, 0, 0, 0, 1, 1, '{}')"),
        ("date_order", "INSERT INTO backtest_runs (kind, name, label_version, period, from_date,"
                       " to_date, capital, slippage_bps, trades, net_profit, costs_paid,"
                       " return_pct, max_drawdown_pct, days, days_without_trade, params)"
                       " VALUES ('strategy', 'x', 'v1', 'walk_forward', '2024-03-06',"
                       " '2024-03-05', 100000, 5, 0, 0, 0, 0, 0, 1, 1, '{}')"),
    ]  # fmt: skip
    for name, sql in bad_rows:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(sql)
        assert name
    # and on the trades: an unknown exit reason and a zero quantity
    for sql in (
        "INSERT INTO backtest_trades (run_id, instrument_id, trade_date, decided_at, side, entry,"
        " exit_price, target, stop, quantity, bars_held, exit_reason, gross_profit, costs,"
        f" net_profit) VALUES ({run_id}, {instrument}, '2024-03-05', now(), 'long', 100, 100,"
        " 100, 99, 10, 1, 'vibes', 0, 0, 0)",
        "INSERT INTO backtest_trades (run_id, instrument_id, trade_date, decided_at, side, entry,"
        " exit_price, target, stop, quantity, bars_held, exit_reason, gross_profit, costs,"
        f" net_profit) VALUES ({run_id}, {instrument}, '2024-03-05', now(), 'long', 100, 100,"
        " 100, 99, 0, 1, 'target', 0, 0, 0)",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(sql)


def test_the_model_registry_accepts_the_intraday_kind(conn):
    """0010 widened model_versions.kind; the daily kinds still work."""
    for kind in ("intraday", "pooled", "sector"):
        conn.execute(
            "INSERT INTO model_versions (name, version, horizon, kind, train_from, train_to,"
            " metrics, beats_baseline, status) VALUES (%s, 1, 10, %s, '2024-01-01',"
            " '2024-06-01', '{}', false, 'candidate')",
            (f"zz_{kind}", kind),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO model_versions (name, version, horizon, kind, train_from, train_to,"
            " metrics, beats_baseline, status) VALUES ('zz_bad', 1, 10, 'daytrading',"
            " '2024-01-01', '2024-06-01', '{}', false, 'candidate')"
        )


def test_an_empty_result_still_stores_a_row(conn):
    """ "No trading" is a baseline and must be recorded like any other."""
    run_id = B.save(conn, B.no_trading(capital=100_000, days=20))
    stored = conn.execute(
        "SELECT kind, name, trades, net_profit, days_without_trade FROM backtest_runs"
        " WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert stored == ("baseline", "no_trading", 0, 0.00, 20)


def test_a_simulated_run_round_trips_through_the_database(conn, instrument):
    """The simulation, the save and the report all agree on the same numbers."""
    import pandas as pd

    rows = pd.DataFrame([
        {"instrument_id": instrument, "trade_date": date(2024, 3, 5),
         "ts": datetime(2024, 3, 5, 4, 0, tzinfo=UTC), "minute": 60, "signal": 1,
         "side": "long", "entry_price": 100.0, "outcome": 1, "gross_return": 0.005,
         "bars_held": 3, "target_pct": 0.5, "stop_pct": 0.25},
    ])  # fmt: skip
    result = B.simulate(
        rows, name="zz_rule", policy=SizingPolicy(capital=100_000), slippage_bps=5.0
    )
    assert len(result.trades) == 1
    run_id = B.save(conn, result)
    net, trades = conn.execute(
        "SELECT net_profit, trades FROM backtest_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert trades == 1
    assert float(net) == pytest.approx(round(result.net_profit, 2))
