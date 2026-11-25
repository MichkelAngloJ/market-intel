"""The event-driven intraday backtest (S5i): sequencing, the daily limits, costs, compounding,
the ruin stop and the metrics. Hand-made rows, so every number can be worked out by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from market_intel import intraday_backtest as B
from market_intel.costs import IntradayCostModel
from market_intel.sizing import OutcomeOdds, SizingPolicy

FREE = IntradayCostModel(
    brokerage_per_order=0.0, brokerage_rate=0.0, stt_rate_sell=0.0, exchange_rate=0.0,
    sebi_per_crore=0.0, stamp_rate_buy=0.0, gst_rate=0.0,
)  # fmt: skip


def row(minute, signal=1, outcome=1, ret=0.005, bars=5, inst=1, day=date(2024, 3, 5), entry=100.0):
    return {
        "instrument_id": inst,
        "trade_date": day,
        "ts": datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC),
        "minute": minute,
        "signal": signal,
        "side": "long" if signal > 0 else "short",
        "entry_price": entry,
        "outcome": outcome,
        "gross_return": ret,
        "bars_held": bars,
        "target_pct": 0.5,
        "stop_pct": 0.25,
    }


def frame(*rows):
    return pd.DataFrame(list(rows))


def run(rows, **kw):
    kw.setdefault("policy", SizingPolicy(capital=1_000_000))
    kw.setdefault("costs", FREE)
    kw.setdefault("slippage_bps", 0.0)
    kw.setdefault("name", "test")
    return B.simulate(rows, **kw)


# --- sequencing and the limits -------------------------------------------------------------


def test_a_signal_while_a_trade_is_open_is_skipped():
    # held for 10 bars from minute 60, so it closes at the end of bar 70: minute 65 is blocked
    result = run(frame(row(60, bars=10), row(65, bars=1), row(70, bars=1)))
    assert [t.bars_held for t in result.trades] == [10, 1]  # minute 60, then minute 70
    assert result.skipped["a trade was already open"] == 1


def test_a_position_frees_the_account_on_its_own_exit_bar():
    # entered at the open of bar 61 and held 5 bars, it closes at the end of bar 65, so a
    # decision at minute 65 (which enters at bar 66) is free to take
    result = run(frame(row(60, bars=5), row(65, bars=1)))
    assert len(result.trades) == 2
    assert not result.skipped


def test_the_maximum_number_of_trades_ends_the_day():
    policy = SizingPolicy(capital=1_000_000, max_trades_per_day=2)
    rows = frame(*[row(m, bars=1) for m in (60, 65, 70, 75)])
    result = run(rows, policy=policy)
    assert len(result.trades) == 2
    assert result.skipped["max trades reached"] == 1  # it breaks out of the day


def test_the_daily_loss_limit_ends_the_day():
    policy = SizingPolicy(
        capital=100_000, risk_per_trade=0.01, daily_loss_limit=0.02, max_trades_per_day=20
    )
    # a 2% stop lets the risk bind rather than the exposure cap: 1% of capital per losing trade
    rows = frame(*[row(m, outcome=-1, ret=-0.02, bars=1) for m in range(60, 120, 5)])
    for r in rows.to_dict("records")[:1]:
        assert r["stop_pct"] == 0.25
    rows["stop_pct"] = 2.0
    result = run(rows, policy=policy)
    assert len(result.trades) == 2  # -1,000 then -1,000 reaches the 2% limit
    assert result.skipped["daily loss limit reached"] == 1
    assert result.net_profit == pytest.approx(-2_000, rel=1e-6)


def test_the_exposure_cap_can_bind_instead_of_the_risk_budget():
    """A tight stop cannot use the whole risk budget: the position would exceed the capital."""
    policy = SizingPolicy(capital=100_000, risk_per_trade=0.01)
    result = run(frame(row(60, outcome=-1, ret=-0.0025, bars=1)), policy=policy)
    trade = result.trades[0]
    assert trade.quantity == 1_000  # 100,000 / 100, not 10,000 / 0.25 = 40,000
    assert trade.net_profit == pytest.approx(-250)  # 0.25% of capital, not the nominal 1%


def test_each_day_starts_with_fresh_limits():
    policy = SizingPolicy(capital=1_000_000, max_trades_per_day=1)
    rows = frame(
        row(60, bars=1, day=date(2024, 3, 5)),
        row(65, bars=1, day=date(2024, 3, 5)),
        row(60, bars=1, day=date(2024, 3, 6)),
    )
    result = run(rows, policy=policy)
    assert len(result.trades) == 2  # one per day
    assert result.days == 2 and result.days_without_trade == 0


def test_a_day_with_no_signal_counts_as_a_day_without_a_trade():
    result = run(frame(row(60, signal=0), row(65, signal=0)))
    assert result.trades == [] and result.days == 1 and result.days_without_trade == 1
    assert result.hit_rate is None and result.sharpe is None


# --- the money ------------------------------------------------------------------------------


def test_profit_quantity_and_exit_price_by_hand():
    policy = SizingPolicy(capital=1_000_000, risk_per_trade=0.01)  # Rs 10,000 at risk
    # entry 100, stop 0.25% away = 0.25 per share -> 10,000 / 0.25 = 40,000 shares,
    # but the exposure cap allows only 1,000,000 / 100 = 10,000 shares
    result = run(frame(row(60, ret=0.005)))
    trade = result.trades[0]
    assert trade.quantity == 10_000
    assert trade.exit_price == pytest.approx(100.5)
    assert trade.gross_profit == pytest.approx(10_000 * 100 * 0.005)  # Rs 5,000
    assert trade.costs == 0.0 and trade.net_profit == pytest.approx(5_000)
    assert trade.exit_reason == "target"
    assert result.return_pct == pytest.approx(0.5)
    assert policy.capital == 1_000_000  # the policy itself is never mutated


def test_a_short_makes_money_when_the_price_falls():
    result = run(frame(row(60, signal=-1, ret=0.005)))
    trade = result.trades[0]
    assert trade.side == "short"
    assert trade.exit_price == pytest.approx(99.5)  # the price fell, the short won
    assert trade.net_profit > 0


def test_real_charges_and_slippage_are_taken_off():
    plain = run(frame(row(60, ret=0.005)))
    charged = run(frame(row(60, ret=0.005)), costs=IntradayCostModel(), slippage_bps=10)
    assert charged.trades[0].gross_profit == pytest.approx(plain.trades[0].gross_profit)
    assert charged.costs_paid > 0
    assert charged.net_profit == pytest.approx(charged.gross_profit - charged.costs_paid, abs=1e-6)
    # 10 bps a side on a Rs 1,000,000 position is Rs 2,000 of slippage alone
    assert charged.costs_paid > 2_000


def test_a_winning_trade_can_still_lose_after_costs():
    # +0.1% gross against a 0.33% round trip: the target was hit and the account still shrank
    result = run(frame(row(60, ret=0.001)), costs=IntradayCostModel(), slippage_bps=10)
    trade = result.trades[0]
    assert trade.exit_reason == "target" and trade.gross_profit > 0
    assert trade.net_profit < 0
    assert result.hit_rate == 0.0  # the hit rate counts money, not barriers


def test_the_exit_reason_follows_the_label_outcome():
    rows = frame(row(60, outcome=1, bars=1), row(65, outcome=-1, ret=-0.0025, bars=1),
                 row(70, outcome=0, ret=0.0001, bars=1))  # fmt: skip
    result = run(rows)
    assert [t.exit_reason for t in result.trades] == ["target", "stop", "time_limit"]


# --- compounding, ruin and metrics ------------------------------------------------------------


def test_compounding_sizes_the_next_day_from_the_equity_reached():
    policy = SizingPolicy(capital=100_000, max_trades_per_day=1)
    rows = frame(row(60, outcome=-1, ret=-0.0025, bars=1, day=date(2024, 3, 5)),
                 row(60, outcome=-1, ret=-0.0025, bars=1, day=date(2024, 3, 6)))  # fmt: skip
    compounded = run(rows, policy=policy, compound=True)
    flat = run(rows, policy=policy, compound=False)
    assert compounded.trades[1].quantity < flat.trades[1].quantity
    assert flat.trades[0].quantity == flat.trades[1].quantity


def test_trading_stops_once_the_account_is_ruined():
    policy = SizingPolicy(capital=100_000, risk_per_trade=1.0, max_trades_per_day=1)
    days = [date(2024, 3, d) for d in (4, 5, 6, 7, 8)]
    rows = frame(*[row(60, outcome=-1, ret=-0.5, bars=1, day=d) for d in days])
    result = run(rows, policy=policy, ruin_fraction=0.25)
    assert result.ruined
    assert len(result.trades) < len(days)
    assert "RUINED" in result.line()


def test_the_equity_curve_drawdown_and_sharpe():
    policy = SizingPolicy(capital=100_000, max_trades_per_day=1)
    # +1,000 then -2,000 then +500 on three days
    rows = frame(
        row(60, ret=0.001, bars=1, day=date(2024, 3, 4), entry=1000.0),
        row(60, outcome=-1, ret=-0.002, bars=1, day=date(2024, 3, 5), entry=1000.0),
        row(60, ret=0.0005, bars=1, day=date(2024, 3, 6), entry=1000.0),
    )
    result = run(rows, policy=policy, compound=False)
    curve = result.equity_curve
    assert len(curve) == 3
    assert curve.iloc[0] > 100_000 and curve.iloc[1] < curve.iloc[0]
    assert result.max_drawdown_pct > 0
    assert result.sharpe is not None
    assert result.days == 3 and result.days_without_trade == 0


def test_no_trading_is_a_baseline_that_cannot_lose():
    result = B.no_trading(capital=100_000, days=50)
    assert result.trades == [] and result.net_profit == 0 and result.max_drawdown_pct == 0
    assert result.days_without_trade == 50
    assert "no_trading" in result.line()
    assert result.row()["kind"] == "baseline"


def test_the_report_table_ranks_by_net_profit():
    a = run(frame(row(60, ret=0.005)), name="winner")
    b = run(frame(row(60, outcome=-1, ret=-0.0025)), name="loser")
    lines = B.report_table([b, a, B.no_trading(1_000_000, 1)])
    assert lines[0].startswith("strategy")
    assert [line.split()[0] for line in lines[2:]] == ["winner", "no_trading", "loser"]


# --- joining signals to labels ------------------------------------------------------------


def labels_frame():
    ts = datetime(2024, 3, 5, 4, 0, tzinfo=UTC)
    return pd.DataFrame([
        {"instrument_id": 1, "ts": ts, "side": "long", "entry_price": 100.0,
         "excluded_reason": None, "outcome": 1, "gross_return": 0.005, "bars_held": 3},
        {"instrument_id": 1, "ts": ts, "side": "short", "entry_price": 100.0,
         "excluded_reason": None, "outcome": -1, "gross_return": -0.0025, "bars_held": 2},
    ])  # fmt: skip


def features_frame():
    return pd.DataFrame([
        {"instrument_id": 1, "trade_date": date(2024, 3, 5), "minute": 60,
         "ts": datetime(2024, 3, 5, 4, 0, tzinfo=UTC)},
    ])  # fmt: skip


def test_prepare_picks_the_label_row_of_the_side_the_signal_points_at():
    long_rows = B.prepare(features_frame(), labels_frame(), pd.Series([1]))
    assert long_rows["side"].tolist() == ["long"] and long_rows["bars_held"].tolist() == [3]
    short_rows = B.prepare(features_frame(), labels_frame(), pd.Series([-1]))
    assert short_rows["side"].tolist() == ["short"] and short_rows["bars_held"].tolist() == [2]
    assert B.prepare(features_frame(), labels_frame(), pd.Series([0])).empty


def test_prepare_drops_rows_the_labels_excluded():
    labels = labels_frame()
    labels["excluded_reason"] = "thin_day"
    assert B.prepare(features_frame(), labels, pd.Series([1])).empty


def test_a_v2_backtest_needs_a_variant():
    with pytest.raises(ValueError, match="variant"):
        B.simulate(frame(row(60)), name="x", label_version="v2",
                   policy=SizingPolicy(capital=100_000))  # fmt: skip


def test_v2_barriers_come_from_the_atr():
    rows = frame(row(60))
    rows = rows.rename(
        columns={
            "outcome": "outcome_k10_b05",
            "gross_return": "gross_return_k10_b05",
            "bars_held": "bars_held_k10_b05",
        }
    )
    rows["atr"] = 1.0  # 1% of a Rs 100 entry
    result = run(rows, label_version="v2", variant="k10_b05")
    trade = result.trades[0]
    assert trade.target == pytest.approx(101.0)  # k=1.0 x ATR above
    assert trade.stop == pytest.approx(99.5)  # k/2 x ATR below


def test_missing_columns_are_reported():
    with pytest.raises(KeyError, match="entry_price"):
        B.simulate(
            pd.DataFrame({"instrument_id": [1], "trade_date": [date(2024, 3, 5)],
                          "minute": [60], "side": ["long"], "signal": [1]}),
            name="x", policy=SizingPolicy(capital=100_000),
        )  # fmt: skip


def test_the_expected_value_gate_only_applies_when_asked():
    # a tiny target that cannot pay the charges
    rows = frame(row(60, ret=0.0005))
    odds = {"long": OutcomeOdds(target=0.3, stop=0.3)}
    gated = run(rows, costs=IntradayCostModel(), slippage_bps=10, ev_gate=True, odds_by_side=odds)
    assert gated.trades == []
    assert gated.skipped["expected gain after costs is not positive"] == 1
    ungated = run(rows, costs=IntradayCostModel(), slippage_bps=10, ev_gate=False)
    assert len(ungated.trades) == 1  # a rule trades its own signal


def test_a_models_probability_becomes_the_sizing_probability():
    rows = frame(row(60))
    rows["probability"] = 0.75
    result = run(rows, kind="model")
    assert result.trades[0].probability == pytest.approx(0.75)
    assert result.row()["kind"] == "model"


def test_an_empty_frame_gives_an_empty_result():
    result = run(pd.DataFrame())
    assert result.trades == [] and result.days == 0
    assert result.equity_curve.empty and result.max_drawdown_pct == 0
    assert np.isclose(result.return_pct, 0.0)
