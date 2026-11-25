"""Intraday charges and capital sizing (S5i). Every number here is worked by hand from the
rates quoted in market_intel.costs (Upstox official pages, checked 2026-11-25)."""

from __future__ import annotations

from datetime import time

import pytest

from market_intel.costs import DEFAULT_INTRADAY_COSTS, IntradayCostModel
from market_intel.sizing import (
    DayState,
    OutcomeOdds,
    SizingPolicy,
    fund_split,
    ipo_lots,
    plan_intraday_trade,
    quantity_for,
    shares_for_amount,
)

# --- intraday costs ------------------------------------------------------------------------


def test_an_intraday_round_trip_by_hand():
    costs = IntradayCostModel()
    # Buy Rs 50,000: brokerage min(20, 50) = 20 + exchange 1.535 + SEBI 0.05 + stamp 1.5
    #                + GST 18% x (20 + 1.535) = 3.8763  ->  26.9613   (no STT on buy)
    assert costs.buy_cost(50_000) == pytest.approx(26.9613, abs=1e-4)
    # Sell Rs 50,000: brokerage 20 + STT 12.5 + exchange 1.535 + SEBI 0.05
    #                 + GST 3.8763  ->  37.9613   (no stamp duty, no DP charge)
    assert costs.sell_cost(50_000) == pytest.approx(37.9613, abs=1e-4)
    assert costs.round_trip(50_000, 50_000) == pytest.approx(64.9226, abs=1e-4)
    # that is 0.1298% of the position: the move a trade must make to pay for itself
    assert costs.breakeven_fraction(50_000) == pytest.approx(0.001298, abs=1e-6)


def test_brokerage_is_the_lower_of_twenty_rupees_and_a_tenth_of_a_percent():
    costs = IntradayCostModel()
    assert costs.brokerage(10_000) == pytest.approx(10.0)  # 0.1% binds below Rs 20,000
    assert costs.brokerage(20_000) == pytest.approx(20.0)  # exactly equal here
    assert costs.brokerage(500_000) == pytest.approx(20.0)  # the flat fee binds above


def test_intraday_costs_less_than_delivery_on_the_same_turnover():
    from market_intel.costs import CostModel

    delivery, intraday = CostModel(), IntradayCostModel()
    value = 100_000
    assert intraday.round_trip(value, value) < (
        delivery.buy_cost(value) + delivery.sell_cost(value)
    )  # no DP charge and STT is 0.025% on one side instead of 0.1% on both


def test_slippage_doubles_the_cost_of_the_round_trip_per_ten_bps():
    costs = IntradayCostModel()
    value = 100_000
    plain = costs.round_trip(value, value)
    with_slip = costs.round_trip(value, value, slippage_bps=10)
    assert with_slip - plain == pytest.approx(2 * value * 10 / 10_000)  # 10 bps on each side


def test_the_round_trip_charges_each_side_on_its_own_turnover():
    costs = IntradayCostModel()
    # a long that wins: bought at 100,000 and sold at 100,500
    both = costs.round_trip(100_000, 100_500)
    assert both == pytest.approx(costs.buy_cost(100_000) + costs.sell_cost(100_500), abs=1e-9)


# --- sizing --------------------------------------------------------------------------------


def test_quantity_comes_from_the_risk_amount_and_the_stop_distance():
    policy = SizingPolicy(capital=1_000_000, risk_per_trade=0.01)  # Rs 10,000 at risk
    quantity, why = quantity_for(policy, entry=1000.0, stop=990.0)  # Rs 10 away
    assert (quantity, why) == (1000, None)  # 10,000 / 10
    assert quantity_for(policy, entry=1000.0, stop=995.0)[0] == 1000  # exposure caps it at 1x


def test_exposure_cap_limits_a_tight_stop():
    policy = SizingPolicy(capital=100_000, max_exposure_per_trade=0.5)
    # risk allows 1,000/3 = 333 shares, but half the capital at Rs 1,200 allows only 41
    quantity, _ = quantity_for(policy, entry=1200.0, stop=1197.0)
    assert quantity == 41


def test_a_stop_at_the_entry_or_a_position_under_one_share_is_refused():
    policy = SizingPolicy(capital=10_000)
    assert quantity_for(policy, 100.0, 100.0) == (0, "stop is at the entry price")
    # Rs 100 of risk with a Rs 500 stop distance cannot buy a whole share
    assert quantity_for(policy, 5000.0, 4500.0)[0] == 0


def test_the_policy_rejects_impossible_settings():
    for kwargs in (
        {"capital": 0},
        {"capital": 100, "risk_per_trade": 0},
        {"capital": 100, "risk_per_trade": 1.5},
        {"capital": 100, "daily_loss_limit": 0},
        {"capital": 100, "max_trades_per_day": 0},
    ):
        with pytest.raises(ValueError):
            SizingPolicy(**kwargs)


def test_outcome_odds_default_to_the_pessimistic_reading():
    assert OutcomeOdds(target=0.4).p_stop == pytest.approx(0.6)  # everything else is the stop
    assert OutcomeOdds(target=0.4).p_time == 0
    odds = OutcomeOdds(target=0.1, stop=0.3)
    assert (odds.p_stop, odds.p_time) == (pytest.approx(0.3), pytest.approx(0.6))
    with pytest.raises(ValueError):
        OutcomeOdds(target=0.8, stop=0.5)


def test_a_plan_states_entry_target_stop_quantity_loss_and_expected_gain():
    policy = SizingPolicy(capital=1_000_000)
    plan = plan_intraday_trade(
        "ZZTEST", "long", 1000.0, target_pct=2.0, stop_pct=1.0,
        odds=OutcomeOdds(target=0.8, stop=0.2), policy=policy,
    )  # fmt: skip
    assert plan.taken and plan.reason is None
    assert (plan.target, plan.stop) == (pytest.approx(1020.0), pytest.approx(990.0))
    assert plan.quantity == 1000  # 10,000 risk / 10 per share
    assert plan.exit_time == time(15, 20)
    # gross = 0.8 x 20 x 1000 - 0.2 x 10 x 1000 = 14,000, well above the costs
    assert plan.details["gross_expected"] == pytest.approx(14_000)
    assert plan.expected_gain == pytest.approx(14_000 - plan.costs)
    assert plan.max_loss == pytest.approx(10 * 1000 + plan.costs)
    assert "x1000 at 1000.00" in plan.lines()[0]


def test_a_short_mirrors_the_barriers_and_can_be_switched_off():
    policy = SizingPolicy(capital=1_000_000)
    plan = plan_intraday_trade(
        "ZZTEST", "short", 1000.0, target_pct=2.0, stop_pct=1.0,
        odds=OutcomeOdds(target=0.8, stop=0.2), policy=policy,
    )  # fmt: skip
    assert (plan.target, plan.stop) == (pytest.approx(980.0), pytest.approx(1010.0))
    off = plan_intraday_trade(
        "ZZTEST", "short", 1000.0, target_pct=2.0, stop_pct=1.0,
        odds=OutcomeOdds(target=0.8), policy=SizingPolicy(capital=1_000_000, allow_short=False),
    )  # fmt: skip
    assert not off.taken and off.reason == "shorting is switched off"


def test_a_trade_that_cannot_pay_its_costs_is_refused():
    policy = SizingPolicy(capital=100_000)
    # the measured v1 base rates: target first 5.5%, stop first 24.7%, the rest time out
    plan = plan_intraday_trade(
        "ZZTEST", "long", 1200.0, target_pct=0.5, stop_pct=0.25,
        odds=OutcomeOdds(target=0.055, stop=0.247), policy=policy, slippage_bps=10,
    )  # fmt: skip
    assert not plan.taken
    assert plan.reason == "expected gain after costs is not positive"
    assert plan.details["expected_gain"] < 0


def test_the_daily_limits_stop_further_trades():
    policy = SizingPolicy(capital=100_000, max_trades_per_day=2, daily_loss_limit=0.02)
    day = DayState()
    assert day.blocked(policy) is None
    day.record(-500.0)
    day.record(-300.0)
    assert day.blocked(policy) == "max trades reached"
    losing = DayState(trades=1, realised=-2_000.0)  # exactly 2% of 100,000
    assert losing.blocked(policy) == "daily loss limit reached"
    plan = plan_intraday_trade(
        "ZZTEST", "long", 100.0, target_pct=2.0, stop_pct=1.0,
        odds=OutcomeOdds(target=0.9), policy=policy, day=losing,
    )  # fmt: skip
    assert not plan.taken and plan.reason == "daily loss limit reached"


def test_an_external_block_is_reported_as_the_reason():
    plan = plan_intraday_trade(
        "ZZTEST", "long", 100.0, target_pct=2.0, stop_pct=1.0, odds=OutcomeOdds(target=0.9),
        policy=SizingPolicy(capital=100_000), blocked="price event on the previous day",
    )  # fmt: skip
    assert not plan.taken and plan.reason == "price event on the previous day"


def test_costs_are_taken_on_the_worse_of_the_two_turnovers():
    policy = SizingPolicy(capital=1_000_000)
    plan = plan_intraday_trade(
        "ZZTEST", "long", 1000.0, target_pct=2.0, stop_pct=1.0,
        odds=OutcomeOdds(target=0.8, stop=0.2), policy=policy,
    )  # fmt: skip
    expected = DEFAULT_INTRADAY_COSTS.round_trip(1000.0 * plan.quantity, 1020.0 * plan.quantity)
    assert plan.costs == pytest.approx(expected)


# --- the other modules' sizing (stubs) -------------------------------------------------------


def test_long_term_stocks_buy_whole_shares_and_at_least_one():
    assert shares_for_amount(10_000, 1200.0) == 8
    assert shares_for_amount(1200, 1200.0) == 1
    assert shares_for_amount(1199, 1200.0) == 0  # cannot afford a single share
    assert shares_for_amount(10_000, 0) == 0


def test_fund_splits_respect_each_minimum():
    funds = ["A", "B", "C"]
    minimums = {"A": 500.0, "B": 500.0, "C": 5_000.0}
    # Rs 3,000 split three ways is 1,000 each: C's 5,000 minimum fails, so it is dropped
    assert fund_split(3_000, funds, minimums) == {"A": 1_500.0, "B": 1_500.0}
    # Rs 30,000 clears every minimum
    assert fund_split(30_000, funds, minimums) == {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0}
    # nothing fits
    assert fund_split(100, funds, minimums) == {}


def test_ipo_applications_are_whole_lots():
    assert ipo_lots(50_000, 14_800.0) == 3
    assert ipo_lots(14_800, 14_800.0) == 1
    assert ipo_lots(10_000, 14_800.0) == 0
