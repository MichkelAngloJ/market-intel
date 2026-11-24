"""Costs (a hand-computed example), folds with purge and embargo, the holdout boundary,
classification metrics, period returns, portfolio math and the beats_baseline rule. Synthetic
numbers only."""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from market_intel import evaluation as ev
from market_intel.costs import CostModel

# --- costs -------------------------------------------------------------------------------


def test_a_delivery_round_trip_by_hand():
    costs = CostModel()
    # Buy Rs 50,000: brokerage 20 + STT 50 + exchange 1.535 + SEBI 0.05 + stamp 7.5
    #                + GST 18% x (20 + 1.535) = 3.8763  ->  82.9613
    assert costs.buy_cost(50_000) == pytest.approx(82.9613, abs=1e-4)
    # Sell Rs 50,000: brokerage 20 + STT 50 + exchange 1.535 + SEBI 0.05 + DP 20
    #                 + GST 18% x (20 + 1.535 + 20) = 7.4763  ->  99.0613
    assert costs.sell_cost(50_000) == pytest.approx(99.0613, abs=1e-4)
    # 10 bps slippage per side adds Rs 50 on each side
    assert costs.buy_cost(50_000, 10) == pytest.approx(132.9613, abs=1e-4)
    assert costs.round_trip_fraction(50_000, 0) == pytest.approx((82.9613 + 99.0613) / 50_000)


def test_slippage_buckets_by_traded_value():
    costs = CostModel()
    crore = 1e7
    assert costs.slippage_for(math.log1p(150 * crore)) == 10
    assert costs.slippage_for(math.log1p(50 * crore)) == 20
    assert costs.slippage_for(math.log1p(2 * crore)) == 40
    assert costs.slippage_for(None) == 40 and costs.slippage_for(float("nan")) == 40


# --- folds, purge, embargo, holdout ------------------------------------------------------


def bdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CAL = bdays(date(2016, 1, 4), 2400)  # ~2016 .. 2025


def test_holdout_is_the_last_six_months_and_folds_stop_before_it():
    usable = CAL[300:-30]
    hold = ev.holdout_start(usable)
    assert hold > usable[-1] - timedelta(days=186) and hold <= usable[-1] - timedelta(days=180)
    folds = ev.make_folds(usable, hold, first_year=2018)
    assert folds[0].name == "2018" and folds[-1].test_end < hold
    assert all(f.test_start.year == f.test_end.year for f in folds)


def test_purge_removes_rows_whose_label_window_reaches_the_test_fold():
    dates = pd.Series(CAL[:800])
    ends = ev.label_end(dates, CAL, 21)
    test_start = date(2018, 1, 1)
    mask = ev.train_mask(dates, ends, test_start, [], CAL)
    kept = dates[mask]
    assert kept.max() < test_start
    assert all(e < test_start for e in ends[mask])
    # the 21 trading days right before the fold are purged: their windows overlap it
    last_kept = kept.max()
    assert sum(1 for d in CAL if last_kept < d < test_start) == 21
    overlapping = dates[
        (dates < test_start) & ends.map(lambda e: e is not None and e >= test_start)
    ]
    assert len(overlapping) == 21 and not mask[overlapping.index].any()


def test_embargo_drops_21_trading_days_after_an_earlier_fold():
    dates = pd.Series(CAL[:1300])
    ends = ev.label_end(dates, CAL, 21)
    f2018 = ev.Fold("2018", date(2018, 1, 1), date(2018, 12, 31))
    mask = ev.train_mask(dates, ends, date(2020, 1, 1), [f2018], CAL)
    after = [d for d in CAL if d > date(2018, 12, 31)][:21]
    assert not mask[dates.isin(after)].any()
    assert mask[dates == [d for d in CAL if d > date(2018, 12, 31)][21]].all()  # day 22 trains
    assert mask[dates.between(date(2018, 1, 1), date(2018, 12, 31))].all()  # fold rows train later


def test_the_holdout_is_never_inside_a_training_window_or_fold():
    usable = CAL[300:-30]
    hold = ev.holdout_start(usable)
    folds = ev.make_folds(usable, hold)
    dates = pd.Series(usable)
    mask = ev.train_mask(dates, ev.label_end(dates, CAL, 21), hold, folds, CAL)
    assert dates[mask].max() < hold and all(f.test_end < hold for f in folds)


# --- metrics -----------------------------------------------------------------------------


def test_classification_metrics_and_calibration():
    y = [True, True, False, False, True]
    p = [0.9, 0.6, 0.4, 0.2, 0.3]
    m = ev.classification_metrics(y, p)
    assert m["auc"] == pytest.approx(5 / 6) and m["accuracy"] == pytest.approx(0.8)
    assert m["precision_up"] == 1.0 and m["brier"] == pytest.approx(
        np.mean((np.array(p) - np.array(y)) ** 2)
    )
    assert ev.classification_metrics(y, [1.0] * 5)["auc"] == 0.5  # always-up has no ranking
    table = ev.calibration_table(y, p)
    assert sum(r["rows"] for r in table) == 5 and table[-1]["bin"] == "0.9-1.0"


# --- portfolio -----------------------------------------------------------------------------


def prices(days, path):
    return pd.DataFrame({"open": path, "close": path}, index=pd.Index(days))


def test_period_returns_use_the_next_open_and_mark_unmeasurable_windows():
    days = bdays(date(2024, 1, 1), 30)
    rising = prices(days, [100.0 + i for i in range(30)])
    split = prices(days, [100.0] * 10 + [50.0] * 20)
    entry_day = prices(days, [100.0] + [50.0] * 29)  # the split is on t+1: entry already after it
    jump = prices(days, [100.0] * 10 + [600.0] * 20)  # +500%: an unrecorded consolidation
    r = ev.period_returns(
        {1: rising, 2: split, 3: entry_day, 4: jump},
        days,
        [days[0]],
        {2: [(days[10], 0.5)], 3: [(days[1], 0.5)]},
        step=21,
    )
    got = dict(zip(r.instrument_id, r.ret, strict=True))
    assert got[1] == pytest.approx(121 / 101 - 1)  # open(t+1) to close(t+21)
    assert math.isnan(got[2]) and math.isnan(got[3]) and math.isnan(got[4])  # unmeasurable


def test_portfolio_math_costs_turnover_and_hits():
    d1, d2 = date(2024, 1, 1), date(2024, 2, 1)
    scores = pd.DataFrame(
        {
            "date": [d1, d1, d1, d2, d2, d2],
            "instrument_id": [1, 2, 3, 1, 2, 3],
            "score": [0.9, 0.8, 0.1, 0.9, 0.1, 0.8],
            "tiebreak": 0.0,
        }
    )
    rets = pd.DataFrame(
        {
            "date": [d1, d1, d1, d2, d2, d2],
            "instrument_id": [1, 2, 3, 1, 2, 3],
            "ret": [0.10, 0.00, -0.5, 0.05, 0.9, -0.05],
        }
    )
    free = CostModel(
        brokerage_per_order=0,
        stt_rate=0,
        exchange_rate=0,
        sebi_per_crore=0,
        stamp_rate=0,
        dp_per_sell=0,
        slippage_bps=(0, 0, 0),
    )
    res = ev.simulate("m", scores, rets, k=2, costs=free)
    assert res.net_returns == pytest.approx([0.05, 0.0])  # {1,2} then {1,3}
    assert res.turnover == pytest.approx((1.0 + 0.5) / 2) and res.hit_rate == pytest.approx(2 / 4)
    assert res.total_return == pytest.approx(0.05)
    paid = ev.simulate("m", scores, rets, k=2, costs=CostModel())
    assert paid.total_return < res.total_return and paid.costs_paid > 0


def test_a_pick_without_a_next_day_open_earns_zero():
    d1 = date(2024, 1, 1)
    scores = pd.DataFrame(
        {"date": [d1, d1], "instrument_id": [1, 2], "score": [0.9, 0.5], "tiebreak": 0.0}
    )
    rets = pd.DataFrame({"date": [d1, d1], "instrument_id": [1, 2], "ret": [float("nan"), 0.2]})
    free = CostModel(
        brokerage_per_order=0,
        stt_rate=0,
        exchange_rate=0,
        sebi_per_crore=0,
        stamp_rate=0,
        dp_per_sell=0,
        slippage_bps=(0, 0, 0),
    )
    assert ev.simulate("m", scores, rets, k=2, costs=free).net_returns == pytest.approx([0.1])


def test_stats_drawdown_and_annualising():
    res = ev._stats("x", [0.1, -0.2, 0.05], [1.0], [0.0], 0.0)
    assert res.max_drawdown == pytest.approx(-0.2)
    assert res.annual_return == pytest.approx((1.1 * 0.8 * 1.05) ** (12 / 3) - 1)


def test_beats_baseline_needs_both_equal_weight_and_the_best_other():
    def r(name, annual):
        return ev.PortfolioResult(name=name, periods=10, annual_return=annual)

    assert ev.beats_baseline(r("m", 0.20), r("ew", 0.10), [r("mom", 0.15), r("n", 0.05)])
    assert not ev.beats_baseline(r("m", 0.12), r("ew", 0.10), [r("mom", 0.15)])  # loses to momentum
    assert not ev.beats_baseline(r("m", 0.12), r("ew", 0.13), [r("mom", 0.05)])  # loses to EW
    assert not ev.beats_baseline(ev.PortfolioResult("m"), r("ew", -0.5), [])  # no periods
