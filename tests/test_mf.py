"""The AMFI parser, the scheme universe and the "how sure" stats engine (S6a).

The stats tests use synthetic NAV series whose answers can be worked out by hand, so a wrong
formula fails rather than merely changing a number nobody can check.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from market_intel import mf

# --- the two AMFI layouts -----------------------------------------------------------------
# Verified live 2026-11-26: the daily file and the history report order their columns
# DIFFERENTLY, which is why the parser reads the header instead of counting positions.
NAVALL = (
    (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;"
        "Option;Net Asset Value;Date"
    )
    + """

Open Ended Schemes(Equity Scheme - Large Cap Fund)

Example Mutual Fund

100001;INF000A00001;-;Example Large Cap Fund;Direct Plan;Growth Option;123.4500;26-Nov-2026
100002;INF000A00002;INF000A00003;Example Large Cap Fund;Regular Plan;IDCW Option;45.6700;26-Nov-2026

Open Ended Schemes(Hybrid Scheme - Aggressive Hybrid Fund)

Other Mutual Fund

100003;INF000A00004;-;Other Balanced Fund;Direct Plan;Growth;99.0000;26-Nov-2026
"""
)

HISTORY = (
    (
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
        "Net Asset Value;Date"
    )
    + """

Open Ended Schemes ( Equity Scheme - Large Cap Fund )

Example Mutual Fund

100001;Example Fund - Direct - Growth;Direct Plan;Growth;INF000A00001;-;120.0000;24-Nov-2026
100001;Example Fund - Direct - Growth;Direct Plan;Growth;INF000A00001;-;123.4500;26-Nov-2026
100009;Legacy Fund - Growth;;;;;10.0000;25-Nov-2026
"""
)


def parse(text: str):
    return list(mf.parse_nav_file(text.splitlines()))


def test_the_daily_file_and_the_history_report_both_parse_despite_different_column_orders():
    daily = {s.scheme_code: (s, n) for s, n in parse(NAVALL)}
    history = {s.scheme_code: (s, n) for s, n in parse(HISTORY)}

    s, n = daily[100001]
    assert s.scheme_name == "Example Large Cap Fund"
    assert s.isin_growth == "INF000A00001"
    assert (n.nav, n.nav_date) == (123.45, date(2026, 11, 26))

    # The SAME scheme out of the history report, where the ISINs come after plan and option.
    h, hn = history[100001]
    assert h.isin_growth == "INF000A00001"
    assert h.plan == "direct" and h.option == "growth"
    assert (hn.nav, hn.nav_date) == (123.45, date(2026, 11, 26))


def test_the_category_heading_and_the_amc_carry_down_to_the_rows_beneath_them():
    rows = {s.scheme_code: s for s, _ in parse(NAVALL)}
    assert rows[100001].amfi_category == "Equity Scheme - Large Cap Fund"
    assert rows[100001].amc == "Example Mutual Fund"
    assert rows[100003].amfi_category == "Hybrid Scheme - Aggressive Hybrid Fund"
    assert rows[100003].amc == "Other Mutual Fund"
    assert rows[100003].category == "hybrid"


def test_every_row_of_a_history_report_is_yielded_not_just_the_last_one():
    rows = [n for _, n in parse(HISTORY) if n.scheme_code == 100001]
    assert [r.nav_date for r in rows] == [date(2026, 11, 24), date(2026, 11, 26)]


def test_an_empty_option_column_falls_back_to_the_scheme_name():
    schemes = {s.scheme_code: s for s, _ in parse(HISTORY)}
    assert schemes[100009].option == "growth"  # only the name says so
    assert schemes[100009].plan == "unknown"


@pytest.mark.parametrize(
    ("category", "name", "expected"),
    [
        ("Equity Scheme - Large Cap Fund", "Anything Fund", "equity"),
        ("Equity Schemes - Large Cap Fund", "Anything Fund", "equity"),  # AMFI writes both
        ("Hybrid Scheme - Arbitrage Fund", "Anything Fund", "hybrid"),
        ("Index Funds - Equity Funds", "Nifty 50 Index Fund", "index"),
        ("Other Scheme - Index Funds", "Sensex Index Fund", "index"),
        ("Equity Scheme - Large Cap Fund", "Nifty 50 Index Fund", "index"),  # filed oddly
        ("Index Funds - Debt Funds", "Gilt Index Fund", "other"),  # a debt index fund is not
        ("Debt Scheme - Liquid Fund", "Liquid Fund", "other"),
        ("Exchange Traded Funds (ETFs) - Equity ETF", "Nifty ETF", "other"),
    ],
)
def test_classification(category, name, expected):
    assert mf.classify(category, name) == expected


def test_the_universe_is_open_ended_equity_hybrid_or_index_growth_only():
    def scheme(**kw):
        base = dict(
            scheme_code=1,
            scheme_name="X",
            amc="A",
            amfi_category="c",
            category="equity",
            scheme_type="open",
            plan="direct",
            option="growth",
            isin_growth=None,
            isin_reinvest=None,
        )
        return mf.Scheme(**{**base, **kw})

    assert mf.in_universe(scheme())
    assert mf.in_universe(scheme(category="hybrid"))
    assert mf.in_universe(scheme(category="index"))
    assert not mf.in_universe(scheme(category="other"))  # debt, ETFs, fund of funds
    assert not mf.in_universe(scheme(option="idcw"))  # IDCW pays out, so its NAV is not total
    assert not mf.in_universe(scheme(option="unknown"))
    assert not mf.in_universe(scheme(scheme_type="close"))


def test_a_row_with_no_published_nav_is_skipped_not_stored_as_zero():
    text = NAVALL.replace("123.4500", "N.A.")
    assert 100001 not in {s.scheme_code for s, _ in parse(text)}


def test_a_file_without_a_recognisable_header_is_refused():
    with pytest.raises(ValueError, match="missing"):
        list(mf.parse_nav_file(["Scheme Code;Something Else", "1;2"]))


# --- the stats engine, on series with answers worked out by hand --------------------------
def daily_series(start: date, navs: list[float]) -> list[tuple[date, float]]:
    """One NAV a day, including weekends: the engine never assumes a trading calendar."""
    return [(start + timedelta(days=i), nav) for i, nav in enumerate(navs)]


def compounding(start: date, days: int, daily_rate: float, first: float = 100.0):
    return [(start + timedelta(days=i), first * (1 + daily_rate) ** i) for i in range(days)]


def test_cagr_of_a_doubling_over_exactly_three_years():
    # 100 -> 200 over 3 years is 2 ** (1/3) - 1 = 25.992%.
    assert mf.cagr(100.0, 200.0, 3.0) == pytest.approx(0.25992105, abs=1e-6)
    assert mf.cagr(100.0, 100.0, 5.0) == pytest.approx(0.0)
    assert mf.cagr(100.0, 50.0, 1.0) == pytest.approx(-0.5)
    assert mf.cagr(100.0, 200.0, 0.0) is None  # no time has passed


def test_rolling_one_year_returns_of_a_steadily_growing_fund_are_all_the_same():
    rows = compounding(date(2015, 1, 1), 365 * 4, 0.0002)
    stats = mf.fund_stats(1, "Steady", "equity", rows, years=(1, 3), levels=())
    one = stats.rolling[1]
    assert one.count > 1000
    # Every window is the same shape, so the whole distribution collapses to one number.
    assert one.worst == pytest.approx(one.best, rel=1e-6)
    assert one.median == pytest.approx((1 + 0.0002) ** 365.25 - 1, rel=1e-3)
    assert stats.rolling[3].count > 0


def test_a_rolling_window_is_not_produced_when_the_history_is_too_short():
    rows = compounding(date(2020, 1, 1), 400, 0.0001)
    stats = mf.fund_stats(1, "Short", "equity", rows, years=(1, 3, 5), levels=())
    assert stats.rolling[1].count > 0
    assert stats.rolling[3].count == 0  # no three-year window exists
    assert stats.rolling[5].count == 0


def test_the_worst_rolling_year_is_the_worst_window_not_the_average():
    # Flat for a year, then a 30% fall over the next year, then flat again.
    navs = [100.0] * 365 + [100.0 - 30.0 * i / 365 for i in range(366)] + [70.0] * 365
    stats = mf.fund_stats(
        1, "Crash", "equity", daily_series(date(2015, 1, 1), navs), years=(1,), levels=()
    )
    one = stats.rolling[1]
    assert one.worst == pytest.approx(-0.30, abs=0.01)
    assert one.best == pytest.approx(0.0, abs=0.01)
    assert one.median is not None and one.worst <= one.median <= one.best


def test_maximum_drawdown_finds_the_deepest_fall_and_whether_it_came_back():
    # 100 up to 120, down to 60 (a 50% fall from the 120 peak), back to 130.
    navs = [100.0, 110.0, 120.0, 90.0, 60.0, 80.0, 120.0, 130.0]
    stats = mf.fund_stats(
        1, "V", "equity", daily_series(date(2020, 1, 1), navs), years=(), levels=()
    )
    dd = stats.max_drawdown
    assert dd.depth == pytest.approx(0.50)
    assert dd.peak_date == date(2020, 1, 3)  # the 120
    assert dd.trough_date == date(2020, 1, 5)  # the 60
    assert dd.recovered_on == date(2020, 1, 7)  # the first day back at 120
    assert dd.recovery_days == 4


def test_a_drawdown_that_never_recovers_says_so_rather_than_guessing():
    navs = [100.0, 120.0, 60.0, 70.0, 80.0]
    stats = mf.fund_stats(
        1, "Down", "equity", daily_series(date(2020, 1, 1), navs), years=(), levels=()
    )
    assert stats.max_drawdown.depth == pytest.approx(0.50)
    assert stats.max_drawdown.recovered_on is None
    assert stats.max_drawdown.recovery_days is None


def test_a_fund_that_only_ever_rises_has_no_drawdown():
    rows = compounding(date(2020, 1, 1), 100, 0.001)
    stats = mf.fund_stats(1, "Up", "equity", rows, years=(), levels=())
    assert stats.max_drawdown.depth == 0.0
    assert stats.max_drawdown.peak_date is None


def test_fall_and_recovery_counts_one_episode_per_slide_not_one_per_day():
    # One long slide 100 -> 70 (30% down), then back to 100 and beyond.
    navs = [100.0] + [100.0 - i for i in range(1, 31)] + [70.0 + i for i in range(1, 32)]
    series = mf.Series.build(daily_series(date(2020, 1, 1), navs))
    ten = mf.fall_and_recovery(series, 0.10)
    assert ten.episodes == 1  # the fall crosses 10% on many days: still ONE episode
    assert ten.recovered == 1
    assert ten.still_down == 0
    assert ten.recovery_rate == 1.0
    assert ten.worst_depth == pytest.approx(0.30)
    assert ten.days.count == 1
    assert ten.days.median == pytest.approx(60.0)  # peak day to the day it got back


def test_a_deeper_level_finds_fewer_episodes_than_a_shallower_one():
    navs = [100.0, 85.0, 100.0, 100.0, 78.0, 101.0, 95.0, 102.0]
    series = mf.Series.build(daily_series(date(2020, 1, 1), navs))
    assert mf.fall_and_recovery(series, 0.10).episodes == 2
    assert mf.fall_and_recovery(series, 0.20).episodes == 1
    assert mf.fall_and_recovery(series, 0.30).episodes == 0


def test_an_unrecovered_fall_is_reported_separately_and_never_averaged_into_the_days():
    # Recovers once quickly, then falls again and stays down.
    navs = [100.0, 80.0, 100.0, 70.0, 72.0, 71.0]
    series = mf.Series.build(daily_series(date(2020, 1, 1), navs))
    r = mf.fall_and_recovery(series, 0.10)
    assert r.episodes == 2
    assert r.recovered == 1
    assert r.still_down == 1
    assert r.recovery_rate == pytest.approx(0.5)
    assert r.days.count == 1  # ONLY the recovered episode: the open one would flatter it
    assert r.days.median == pytest.approx(2.0)


def test_recovery_is_measured_back_to_the_peak_not_to_the_trough():
    navs = [100.0, 80.0, 90.0, 95.0, 99.0, 100.0]
    series = mf.Series.build(daily_series(date(2020, 1, 1), navs))
    r = mf.fall_and_recovery(series, 0.10)
    assert r.recovered == 1
    assert r.days.median == pytest.approx(5.0)  # day 0 (the peak) to day 5 (back at 100)


def test_annualised_volatility_of_a_perfectly_steady_fund_is_zero():
    rows = compounding(date(2020, 1, 1), 300, 0.0005)
    assert mf.annualised_volatility(mf.Series.build(rows)) == pytest.approx(0.0, abs=1e-9)


def test_annualised_volatility_scales_a_known_daily_wobble_by_sqrt_252():
    # Alternating +1%/-1% log steps: the daily standard deviation is 0.01 by construction.
    navs = [100.0 * math.exp(0.01 * (-1) ** i) for i in range(500)]
    vol = mf.annualised_volatility(mf.Series.build(daily_series(date(2020, 1, 1), navs)))
    assert vol == pytest.approx(0.01 * math.sqrt(252) * 2, rel=0.02)


def test_the_series_sorts_and_deduplicates_whatever_order_the_rows_arrive_in():
    rows = [(date(2020, 1, 3), 3.0), (date(2020, 1, 1), 1.0), (date(2020, 1, 3), 9.0)]
    s = mf.Series.build(rows)
    assert s.dates == (date(2020, 1, 1), date(2020, 1, 3))
    assert s.navs[-1] == 9.0  # the last value for a date wins


def test_percentile_interpolates_and_handles_a_single_value():
    assert mf.percentile([1.0], 0.5) == 1.0
    assert mf.percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert mf.percentile([0.0, 5.0, 10.0], 0.9) == pytest.approx(9.0)


def test_ranking_is_within_a_category_and_does_not_depend_on_the_input_order():
    def stats_for(code, category, rate):
        rows = compounding(date(2018, 1, 1), 1000, rate)
        return mf.fund_stats(code, f"F{code}", category, rows, years=(), levels=())

    funds = [
        stats_for(1, "equity", 0.0001),
        stats_for(2, "equity", 0.0005),
        stats_for(3, "hybrid", 0.0002),
    ]
    ranks = mf.rank_within_category(funds)
    assert ranks[2] > ranks[1]
    assert ranks[3] == pytest.approx(0.5)  # the only fund in its category ranks mid, not top
    assert mf.rank_within_category(list(reversed(funds))) == ranks


def test_identical_funds_rank_equal_rather_than_by_accident_of_order():
    rows = compounding(date(2018, 1, 1), 500, 0.0003)
    funds = [mf.fund_stats(c, f"F{c}", "equity", rows, years=(), levels=()) for c in (1, 2, 3)]
    ranks = mf.rank_within_category(funds)
    assert ranks[1] == ranks[2] == ranks[3] == pytest.approx(0.5)


def test_a_fund_with_no_measurable_value_is_left_out_rather_than_ranked_last():
    one_point = mf.fund_stats(9, "New", "equity", [(date(2026, 11, 26), 10.0)], years=(), levels=())
    assert one_point.cagr_since_start is None
    good = mf.fund_stats(
        1, "Old", "equity", compounding(date(2018, 1, 1), 500, 0.0003), years=(), levels=()
    )
    ranks = mf.rank_within_category([good, one_point])
    assert 9 not in ranks
    assert 1 in ranks


def test_every_stats_output_carries_the_disclaimer_and_the_survivorship_note():
    assert "not indicative of future results" in mf.DISCLAIMER
    assert "not investment advice" in mf.DISCLAIMER
    assert "merged" in mf.SURVIVORSHIP_NOTE and "wound up" in mf.SURVIVORSHIP_NOTE
