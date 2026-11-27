"""The three mutual-fund decision commands: lumpsum, SIP and keep-or-exit (S6b).

Everything here runs on synthetic NAV whose answer can be worked out by hand, so a wrong
formula fails rather than merely changing a number nobody can check. The engine itself is
offline and deterministic, which is what makes that possible.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from market_intel import mf, mf_cli

# --- helpers ------------------------------------------------------------------------------


def daily(start: date, navs: list[float]) -> mf.Series:
    """One NAV a day, weekends included: nothing here assumes a trading calendar."""
    return mf.Series.build([(start + timedelta(days=i), nav) for i, nav in enumerate(navs)])


def growing(start: date, days: int, daily_rate: float, first: float = 100.0) -> mf.Series:
    return daily(start, [first * (1 + daily_rate) ** i for i in range(days)])


def fund(
    code: int,
    *,
    years: float,
    cagr: float | None = 0.12,
    volatility: float | None = 0.15,
    fall: float = 0.20,
    median_5y: float | None = 0.11,
    worst_3y: float | None = 0.02,
    category: str = "equity",
    start: date = date(2010, 1, 1),
) -> mf.FundStats:
    """A FundStats built directly, so a ranking test is about ranking and nothing else."""
    return mf.FundStats(
        scheme_code=code,
        scheme_name=f"Fund {code}",
        category=category,
        start=start,
        end=start + timedelta(days=int(years * 365.25)),
        observations=int(years * 365),
        years=years,
        cagr_since_start=cagr,
        volatility=volatility,
        max_drawdown=mf.Drawdown(depth=fall),
        rolling={
            3: mf.Distribution.of([worst_3y, worst_3y + 0.10] if worst_3y is not None else []),
            5: mf.Distribution.of(
                [median_5y - 0.05, median_5y, median_5y + 0.05] if median_5y is not None else []
            ),
        },
    )


# --- the minimum-history rule -------------------------------------------------------------


@pytest.mark.parametrize(
    ("years", "grade"),
    [
        (15.9, mf.RANKED),
        (5.0, mf.RANKED),
        (4.999, mf.SHORT_HISTORY),
        (3.0, mf.SHORT_HISTORY),
        (2.999, mf.TOO_NEW),
        (0.0, mf.TOO_NEW),
    ],
)
def test_the_history_rule_has_exactly_three_groups_with_the_boundaries_included(years, grade):
    assert mf.history_grade(years) == grade


def test_a_fund_with_too_little_history_is_never_ranked_whatever_its_numbers_look_like():
    brilliant_but_new = fund(1, years=2.5, cagr=0.60, fall=0.05, volatility=0.10)
    ordinary_but_old = fund(2, years=12.0, cagr=0.11)
    keep, dropped = mf.eligible([brilliant_but_new, ordinary_but_old], mf.RISK_BANDS["high"])
    assert [s.scheme_code for s in keep] == [2]
    assert dropped[0][0].scheme_code == 1
    assert mf.TOO_NEW in dropped[0][1][0]


def test_every_fund_left_out_carries_the_reason_it_was_left_out():
    wild = fund(1, years=10.0, volatility=0.40, fall=0.60)
    keep, dropped = mf.eligible([wild], mf.RISK_BANDS["low"])
    assert keep == []
    (_stats, why) = dropped[0]
    assert any("volatility" in reason for reason in why)
    assert any("below a peak" in reason for reason in why)


def test_the_high_band_sets_no_ceiling_so_only_history_can_exclude_a_fund():
    wild = fund(1, years=10.0, volatility=0.95, fall=0.90)
    keep, _dropped = mf.eligible([wild], mf.RISK_BANDS["high"])
    assert [s.scheme_code for s in keep] == [1]


def test_a_fund_whose_volatility_cannot_be_measured_is_outside_a_band_that_has_a_ceiling():
    unknown = fund(1, years=10.0, volatility=None)
    assert mf.RISK_BANDS["medium"].outside(unknown)
    assert not mf.RISK_BANDS["high"].outside(unknown)


# --- the lumpsum ranking ------------------------------------------------------------------


def test_the_ranking_does_not_depend_on_the_order_the_funds_arrive_in():
    funds = [
        fund(1, years=10.0, cagr=0.18, fall=0.30, median_5y=0.17, worst_3y=0.05),
        fund(2, years=10.0, cagr=0.12, fall=0.20, median_5y=0.11, worst_3y=0.02),
        fund(3, years=10.0, cagr=0.09, fall=0.45, median_5y=0.08, worst_3y=-0.04),
    ]
    forward = [r.stats.scheme_code for r in mf.rank_lumpsum(funds)]
    backward = [r.stats.scheme_code for r in mf.rank_lumpsum(list(reversed(funds)))]
    # 1 and 2 earned the same return per unit of fall (0.18/0.30 and 0.12/0.20 are both 0.6),
    # so that measure ties and the other two put 1 ahead.
    assert forward == backward == [1, 2, 3]


def test_identical_funds_get_identical_scores_and_are_ordered_by_scheme_code():
    same = [fund(code, years=10.0) for code in (7, 3, 5)]
    ranked = mf.rank_lumpsum(same)
    assert {round(r.score, 12) for r in ranked} == {0.5}
    assert [r.stats.scheme_code for r in ranked] == [3, 5, 7]


def test_a_fund_paid_better_for_the_same_fall_ranks_above_one_that_was_not():
    patient = fund(1, years=10.0, cagr=0.20, fall=0.40, median_5y=0.19, worst_3y=0.03)
    punished = fund(2, years=10.0, cagr=0.10, fall=0.40, median_5y=0.09, worst_3y=0.01)
    assert [r.stats.scheme_code for r in mf.rank_lumpsum([punished, patient])] == [1, 2]


def test_return_per_fall_is_the_annual_return_divided_by_the_deepest_fall():
    measures = mf.lumpsum_measures(fund(1, years=10.0, cagr=0.15, fall=0.30))
    assert measures["return_per_fall"] == pytest.approx(0.5)


def test_a_fund_that_never_fell_has_no_return_per_fall_rather_than_an_infinite_one():
    measures = mf.lumpsum_measures(fund(1, years=10.0, cagr=0.15, fall=0.0))
    assert measures["return_per_fall"] is None


def test_a_fund_missing_one_measure_is_still_ranked_on_the_ones_it_has():
    complete = fund(1, years=10.0)
    partial = fund(2, years=10.0, median_5y=None)
    ranked = {r.stats.scheme_code: r for r in mf.rank_lumpsum([complete, partial])}
    assert set(ranked[1].percentiles) == set(mf.LUMPSUM_MEASURES)
    assert "median_5y" not in ranked[2].percentiles
    assert 0.0 <= ranked[2].score <= 1.0


# --- choosing which funds a SIP goes into --------------------------------------------------


def test_the_plan_never_takes_two_funds_from_the_same_fund_house():
    ranked = mf.rank_lumpsum(
        [
            fund(1, years=10.0, cagr=0.20, median_5y=0.19, worst_3y=0.06),
            fund(2, years=10.0, cagr=0.19, median_5y=0.18, worst_3y=0.05),
            fund(3, years=10.0, cagr=0.10, median_5y=0.09, worst_3y=0.01),
        ]
    )
    houses = {1: "One AMC", 2: "One AMC", 3: "Another AMC"}
    picked = mf.diversify(ranked, 2, houses)
    assert [r.stats.scheme_code for r in picked] == [1, 3]


def test_the_categories_are_spread_before_any_category_gets_a_second_fund():
    ranked = mf.rank_lumpsum(
        [
            fund(1, years=10.0, cagr=0.20, median_5y=0.19, worst_3y=0.06, category="equity"),
            fund(2, years=10.0, cagr=0.19, median_5y=0.18, worst_3y=0.05, category="equity"),
            fund(3, years=10.0, cagr=0.10, median_5y=0.09, worst_3y=0.01, category="hybrid"),
        ]
    )
    picked = mf.diversify(ranked, 2, {})
    assert [r.stats.scheme_code for r in picked] == [1, 3]


def test_a_fund_is_never_picked_twice_even_when_more_slots_than_funds_are_asked_for():
    ranked = mf.rank_lumpsum([fund(1, years=10.0), fund(2, years=10.0)])
    assert len(mf.diversify(ranked, 5, {})) == 2


# --- splitting the monthly amount ----------------------------------------------------------


def test_the_instalments_add_up_to_exactly_what_the_owner_asked_for():
    ranked = mf.rank_lumpsum([fund(code, years=10.0) for code in (1, 2, 3)])
    plan = mf.split_sip(ranked, 10_000)
    assert len(plan.slices) == 3
    assert sum(s.monthly for s in plan.slices) == pytest.approx(10_000)
    # 10,000 over three funds is 3,333.33; rounded down to whole hundreds that is 3,300, and
    # the 100 left over goes to the top-ranked fund.
    assert sorted(s.monthly for s in plan.slices) == [3_300, 3_300, 3_400]
    assert plan.slices[0].monthly == 3_400


def test_the_plan_holds_fewer_funds_rather_than_breaking_a_minimum():
    ranked = mf.rank_lumpsum([fund(code, years=10.0) for code in (1, 2, 3)])
    plan = mf.split_sip(ranked, 1_200)  # 400 each would be under the 500 default
    assert len(plan.slices) == 2
    assert all(s.monthly >= s.minimum for s in plan.slices)
    assert plan.notes


def test_a_scheme_with_its_own_higher_minimum_is_respected():
    ranked = mf.rank_lumpsum([fund(code, years=10.0) for code in (1, 2)])
    plan = mf.split_sip(ranked, 3_000, minimums={2: 2_000})
    assert [s.scheme_code for s in plan.slices] == [1]
    assert plan.slices[0].monthly == pytest.approx(3_000)


def test_an_amount_below_every_minimum_produces_no_plan_and_says_so():
    ranked = mf.rank_lumpsum([fund(1, years=10.0)])
    plan = mf.split_sip(ranked, 100)
    assert plan.slices == ()
    assert "below the minimum" in plan.notes[0]


# --- the SIP arithmetic, against answers worked out by hand --------------------------------


def test_a_sip_into_a_flat_fund_returns_exactly_what_was_paid_in():
    flat = daily(date(2015, 1, 1), [100.0] * 800)
    run = mf.sip_run([(1_000.0, flat)], date(2015, 1, 1), 2)
    assert run is not None
    invested, value, rate = run
    assert invested == pytest.approx(24_000)  # 24 monthly instalments
    assert value == pytest.approx(24_000)
    assert rate == pytest.approx(0.0, abs=1e-6)


def test_a_sip_into_a_fund_that_doubles_in_a_straight_line_buys_more_units_when_it_is_cheap():
    # 100 to 200 over 400 days. 12 monthly instalments of 1,200 in the first year.
    climbing = daily(date(2015, 1, 1), [100.0 + 100.0 * i / 399 for i in range(400)])
    run = mf.sip_run([(1_200.0, climbing)], date(2015, 1, 1), 1)
    assert run is not None
    invested, value, rate = run
    assert invested == pytest.approx(14_400)
    # Averaging in beats the fund's own rise, because the early units were cheaper: the fund
    # itself gained about 91% over the year, the SIP cannot have gained that much.
    assert 14_400 < value < 14_400 * 1.91
    assert rate is not None and rate > 0


def test_a_window_a_fund_does_not_fully_cover_is_dropped_rather_than_patched():
    short = daily(date(2020, 1, 1), [100.0] * 200)
    assert mf.sip_run([(1_000.0, short)], date(2019, 1, 1), 1) is None  # starts too late
    assert mf.sip_run([(1_000.0, short)], date(2020, 1, 1), 3) is None  # ends too early


def test_every_rolling_window_of_a_flat_fund_gives_the_same_answer():
    flat = daily(date(2015, 1, 1), [50.0] * 1_500)
    outcome = mf.sip_windows([(1_000.0, flat)], 2)
    assert outcome.windows > 12
    assert outcome.multiple.worst == pytest.approx(1.0)
    assert outcome.multiple.best == pytest.approx(1.0)
    assert outcome.invested == pytest.approx(24_000)


def test_a_sip_across_two_funds_pays_both_and_values_both():
    flat = daily(date(2015, 1, 1), [100.0] * 800)
    also_flat = daily(date(2015, 1, 1), [25.0] * 800)
    run = mf.sip_run([(600.0, flat), (400.0, also_flat)], date(2015, 1, 1), 2)
    assert run is not None
    invested, value, _rate = run
    assert invested == pytest.approx(24_000)
    assert value == pytest.approx(24_000)


def test_a_plan_whose_funds_share_no_long_enough_history_reports_no_windows_not_a_guess():
    short = daily(date(2024, 1, 1), [100.0] * 200)
    outcome = mf.sip_windows([(1_000.0, short)], 10)
    assert outcome.windows == 0
    assert outcome.annualised.count == 0


# --- the annualised rate -------------------------------------------------------------------


def test_one_payment_that_comes_back_ten_percent_bigger_a_year_later_annualises_near_ten():
    rate = mf.xirr([(date(2021, 1, 1), -100.0), (date(2022, 1, 1), 110.0)])
    assert rate is not None and rate == pytest.approx(0.10, abs=0.002)


def test_money_that_never_comes_back_has_no_rate_in_the_search_range():
    assert mf.xirr([(date(2021, 1, 1), -100.0), (date(2022, 1, 1), 0.0)]) is None


def test_a_rate_is_a_rate_whatever_the_size_of_the_flows():
    small = mf.xirr([(date(2021, 1, 1), -1.0), (date(2024, 1, 1), 2.0)])
    large = mf.xirr([(date(2021, 1, 1), -1_000_000.0), (date(2024, 1, 1), 2_000_000.0)])
    assert small is not None and large is not None
    assert small == pytest.approx(large, abs=1e-9)
    assert small == pytest.approx(2 ** (1 / 3.0) - 1, abs=0.002)


def test_the_months_of_a_mandate_fall_on_the_same_day_and_short_months_use_the_28th():
    months = mf.month_starts(date(2020, 1, 31), date(2020, 4, 30), 31)
    # February and April have no 31st, so the mandate runs on the 28th, which is what an AMC
    # does rather than skipping the month.
    assert months == [
        date(2020, 1, 31),
        date(2020, 2, 28),
        date(2020, 3, 31),
        date(2020, 4, 28),
    ]


# --- keep or exit --------------------------------------------------------------------------


def test_the_position_says_how_far_it_fell_and_how_far_it_has_to_rise_to_get_back():
    position = mf.Position(invested=200_000, current=150_000)
    assert position.change == pytest.approx(-0.25)
    # Down a quarter needs a third back, which is the asymmetry the owner should see.
    assert position.gain_needed == pytest.approx(1 / 3, abs=1e-9)


def test_a_holding_at_a_gain_needs_no_gain_to_get_back():
    position = mf.Position(invested=100_000, current=120_000)
    assert position.change == pytest.approx(0.20)
    assert position.gain_needed == pytest.approx(-1 / 6, abs=1e-9)


def test_the_depth_matched_against_history_is_the_deeper_of_the_two_falls():
    assert mf.match_level(0.30, -0.10) == pytest.approx(0.30)  # the fund fell further
    assert mf.match_level(0.10, -0.28) == pytest.approx(0.28)  # the owner is down further


def test_nothing_is_matched_when_the_holding_is_barely_down():
    assert mf.match_level(0.02, -0.01) is None
    assert mf.match_level(0.0, 0.15) is None


def test_a_fall_and_its_recovery_are_found_with_the_peak_and_the_days_it_took():
    # 100 up to 120, down to 84 (30% below the peak), back to 120 and on.
    navs = [100.0 + i for i in range(21)]  # 100..120, peaking on day 20
    navs += [120.0 - 1.2 * i for i in range(1, 31)]  # down to 84 by day 50
    navs += [84.0 + 1.2 * i for i in range(1, 41)]  # back through 120 by day 80
    episodes = mf.fall_episodes(daily(date(2020, 1, 1), navs), 0.20)
    assert len(episodes) == 1
    (episode,) = episodes
    assert episode.peak_date == date(2020, 1, 21)  # day 20, the peak it fell from
    assert episode.depth == pytest.approx(0.30, abs=0.005)
    assert episode.recovered_on == date(2020, 3, 21)  # day 80
    assert episode.days == 60


def test_a_fall_still_under_water_has_no_recovery_days_at_all():
    navs = [100.0 + i for i in range(21)] + [120.0 - 1.2 * i for i in range(1, 31)]
    (episode,) = mf.fall_episodes(daily(date(2020, 1, 1), navs), 0.20)
    assert episode.recovered_on is None
    assert episode.days is None


def test_the_recovery_evidence_splits_the_recoveries_by_the_remaining_horizon():
    episodes = [
        mf.Episode(date(2020, 1, 1), 100.0, date(2020, 2, 1), 0.3, date(2020, 7, 1)),  # 182d
        mf.Episode(date(2021, 1, 1), 100.0, date(2021, 2, 1), 0.3, date(2024, 1, 1)),  # 1096d
        mf.Episode(date(2025, 1, 1), 100.0, date(2025, 2, 1), 0.3, None),  # still down
    ]
    evidence = mf.recovery_evidence(episodes, 0.30, horizon_days=365)
    assert evidence.episodes == 3
    assert evidence.recovered == 2
    assert evidence.still_down == 1
    assert evidence.within_horizon == 1
    assert evidence.beyond_horizon == 1
    assert evidence.recovery_rate == pytest.approx(2 / 3)


def test_a_fall_still_under_water_is_never_averaged_into_the_recovery_times():
    episodes = [
        mf.Episode(date(2020, 1, 1), 100.0, date(2020, 2, 1), 0.3, date(2020, 3, 1)),  # 60d
        mf.Episode(date(2025, 1, 1), 100.0, date(2025, 2, 1), 0.9, None),
    ]
    evidence = mf.recovery_evidence(episodes, 0.30, horizon_days=None)
    assert evidence.days.count == 1
    assert evidence.days.median == pytest.approx(60.0)
    assert evidence.still_down == 1


def test_the_fall_counting_is_unchanged_by_the_episode_refactor():
    """fall_and_recovery is now built on fall_episodes: the two must agree exactly."""
    navs = [100.0 + i for i in range(21)]
    navs += [120.0 - 1.2 * i for i in range(1, 31)]
    navs += [84.0 + 1.2 * i for i in range(1, 41)]
    series = daily(date(2020, 1, 1), navs)
    counted = mf.fall_and_recovery(series, 0.20)
    episodes = mf.fall_episodes(series, 0.20)
    assert counted.episodes == len(episodes)
    assert counted.recovered == sum(1 for e in episodes if e.recovered_on)
    assert counted.days.median == pytest.approx(float(episodes[0].days))


# --- where a fund stands today -------------------------------------------------------------


def test_a_fund_at_its_highest_nav_is_nothing_below_its_peak():
    peak = mf.distance_below_peak(growing(date(2015, 1, 1), 500, 0.001))
    assert peak.below == pytest.approx(0.0)
    assert peak.peak_date == date(2015, 1, 1) + timedelta(days=499)


def test_a_fund_that_has_slipped_reports_how_far_below_its_highest_nav_it_is():
    series = daily(date(2020, 1, 1), [100.0, 150.0, 120.0])
    peak = mf.distance_below_peak(series)
    assert peak.below == pytest.approx(0.20)
    assert peak.peak_date == date(2020, 1, 2)
    assert peak.peak_nav == pytest.approx(150.0)


def test_the_stats_engine_carries_the_distance_below_the_peak_with_everything_else():
    stats = mf.fund_stats(
        1,
        "F",
        "equity",
        [(date(2020, 1, 1), 100.0), (date(2020, 1, 2), 150.0), (date(2020, 1, 3), 120.0)],
        years=(),
        levels=(),
    )
    assert stats.peak is not None
    assert stats.peak.below == pytest.approx(0.20)


# --- what every report has to say ----------------------------------------------------------


def test_every_report_ends_with_the_survivorship_note_and_the_disclaimer():
    footer = "\n".join(mf_cli.report_footer())
    assert mf.SURVIVORSHIP_NOTE in footer
    assert "not investment advice" in footer


def test_the_minimum_history_rule_is_printed_wherever_a_ranking_is():
    note = "\n".join(mf_cli.history_note())
    for grade in (mf.RANKED, mf.SHORT_HISTORY, mf.TOO_NEW):
        assert mf.GRADE_NOTE[grade] in note
    assert "too new to judge" in note


def test_the_stop_rule_names_the_horizon_the_goal_and_the_review_and_refuses_to_stop_on_a_fall():
    lines = "\n".join(mf.stop_rule_lines(years=10, goal=5_000_000))
    assert "10 years" in lines
    assert "5,000,000" in lines
    assert "6 monthly reviews" in lines
    assert "A fall in the market is NOT on this list" in lines


def test_the_stop_rule_still_has_a_goal_line_when_no_goal_was_given():
    lines = "\n".join(mf.stop_rule_lines(years=5, goal=None))
    assert "--goal" in lines


def test_the_keep_or_exit_trade_offs_never_promise_a_recovery():
    text = " ".join(mf.KEEP_OR_EXIT_TRADE_OFFS).casefold()
    assert "neither column is a recommendation" in text
    assert "the decision is the owner's" in text
    assert "not a schedule" in text
    # The only place a recovery is mentioned is the sentence that refuses to promise one.
    assert text.count("will recover") == 1
    assert "nothing here says this fund will recover" in text


def test_the_minimum_sip_is_declared_as_an_input_because_amfi_does_not_publish_it():
    assert "AMFI's NAV files do not carry" in mf.MIN_SIP_NOTE
    assert f"{mf.DEFAULT_MIN_SIP:,.0f}" in mf.MIN_SIP_NOTE


def test_days_to_recover_are_labelled_fastest_and_slowest_not_worst_and_best():
    line = mf_cli.distribution_line(
        "days", mf.Distribution.of([10.0, 900.0]), as_percent=False, low_is_better=True
    )
    assert "fastest" in line and "slowest" in line
    assert "worst" not in line


def test_a_returns_distribution_keeps_worst_and_best():
    line = mf_cli.distribution_line("1-year", mf.Distribution.of([-0.3, 0.4]))
    assert "worst" in line and "best" in line


def test_the_scheme_minimum_argument_rejects_anything_that_is_not_code_equals_amount():
    assert mf_cli.min_sip_pair("118989=1000") == (118989, 1000.0)
    with pytest.raises(Exception, match="CODE=AMOUNT"):
        mf_cli.min_sip_pair("118989")


def test_the_risk_bands_get_stricter_in_one_direction_only():
    low, medium, high = (mf.RISK_BANDS[name] for name in ("low", "medium", "high"))
    assert low.max_volatility is not None and medium.max_volatility is not None
    assert low.max_volatility < medium.max_volatility
    assert low.max_fall is not None and medium.max_fall is not None
    assert low.max_fall < medium.max_fall
    assert high.max_volatility is None and high.max_fall is None


def test_the_ranking_measures_all_have_a_note_a_report_can_print():
    assert set(mf.LUMPSUM_MEASURES) == set(mf.MEASURE_NOTE)
    assert all(mf.MEASURE_NOTE[name] for name in mf.LUMPSUM_MEASURES)


def test_a_sip_outcome_with_no_windows_is_empty_rather_than_zero():
    outcome = mf.sip_windows([], 5)
    assert outcome.windows == 0
    assert outcome.value.count == 0
    assert math.isclose(outcome.invested, 0.0)


def test_the_ranking_commands_look_at_direct_plans_unless_told_otherwise():
    """One fund is two scheme codes - Direct and Regular - and nobody buys both."""
    parser = mf_cli.build_parser()
    lump = parser.parse_args(["lumpsum", "--amount", "100000", "--risk", "medium"])
    assert lump.plan == "direct"
    sip = parser.parse_args(["sip", "--amount", "5000", "--years", "5", "--risk", "low"])
    assert sip.plan == "direct"
    both = parser.parse_args(["lumpsum", "--amount", "100000", "--risk", "high", "--plan", "any"])
    assert both.plan == "any"


def test_risk_is_required_because_it_is_the_owners_comfort_and_not_a_guess():
    parser = mf_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["lumpsum", "--amount", "100000"])
    with pytest.raises(SystemExit):
        parser.parse_args(["sip", "--amount", "5000", "--years", "5"])


def test_keep_or_exit_needs_a_value_or_a_buy_date_and_refuses_without_either(capsys):
    assert mf_cli.main(["keep-or-exit", "--fund", "118989", "--invested", "100000"]) == 1
    assert "--current or --buy-date" in capsys.readouterr().err
