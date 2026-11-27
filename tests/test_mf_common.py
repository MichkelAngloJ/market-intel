"""Comparing funds of different ages on the same market (S6c).

S6b ranked every fund on its OWN history, so a fund whose NAV began at the bottom of a crash
had a flattering worst case and could top a table without ever having been through a fall.
These tests pin the three things that fix it: one shared window, a flag for funds that have
never seen a decline, and windows that do not overlap.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from market_intel import mf, mf_cli

# --- helpers ------------------------------------------------------------------------------


def daily(start: date, navs: list[float]) -> mf.Series:
    return mf.Series.build([(start + timedelta(days=i), nav) for i, nav in enumerate(navs)])


def steady(start: date, days: int, daily_rate: float, first: float = 100.0) -> mf.Series:
    return daily(start, [first * (1 + daily_rate) ** i for i in range(days)])


def crashing(start: date, days: int, *, at: int, depth: float, back_by: int) -> mf.Series:
    """A rising series with one deliberate fall, so there is a decline to be tested by."""
    navs = []
    for i in range(days):
        level = 100.0 * (1.0 + 0.0003) ** i
        if at <= i < at + 100:
            level *= 1.0 - depth * (i - at) / 100
        elif at + 100 <= i < back_by:
            level *= (1.0 - depth) + depth * (i - at - 100) / (back_by - at - 100)
        navs.append(level)
    return daily(start, navs)


def _calm(base: float, day: int, *, rate: float, dip: float = 0.06, at: int = 600) -> float:
    """A rising stretch with one ordinary 6% dip - calm, but not so flat it never falls."""
    level = base * (1.0 + rate) ** (day + 1)
    if at <= day < at + 60:
        level *= 1.0 - dip * (day - at) / 60
    elif at + 60 <= day < at + 180:
        level *= (1.0 - dip) + dip * (day - at - 60) / 120
    return level


def span(code: int, first: date, last: date) -> mf.Span:
    return mf.Span(code, first, last)


# --- the shared window ---------------------------------------------------------------------


def test_the_window_is_the_longest_stretch_every_eligible_fund_covers():
    end = date(2026, 1, 1)
    spans = [
        span(1, date(2011, 1, 1), end),
        span(2, date(2015, 6, 1), end),
        span(3, date(2018, 3, 15), end),  # the latest start sets the window
    ]
    window, covering = mf.common_window(spans)
    assert window.start == date(2018, 3, 15)
    assert window.end == end
    assert covering == [1, 2, 3]
    assert window.funds == 3


def test_a_fund_with_too_little_history_does_not_get_to_shorten_the_window():
    end = date(2026, 1, 1)
    spans = [
        span(1, date(2011, 1, 1), end),
        span(2, date(2015, 6, 1), end),
        span(3, date(2024, 1, 1), end),  # under five years: not eligible, so not a constraint
    ]
    window, covering = mf.common_window(spans)
    assert window.start == date(2015, 6, 1)
    assert covering == [1, 2]


def test_a_fund_that_stopped_reporting_cannot_drag_the_window_backwards():
    end = date(2026, 1, 1)
    spans = [
        span(1, date(2011, 1, 1), end),
        span(2, date(2012, 1, 1), end),
        span(3, date(2011, 1, 1), date(2019, 5, 2)),  # dead for years
    ]
    window, covering = mf.common_window(spans)
    assert window.end == end  # not 2019
    assert covering == [1, 2]
    assert window.stale == 1


def test_asking_for_a_longer_window_drops_the_funds_that_cannot_cover_it():
    end = date(2026, 1, 1)
    spans = [
        span(1, date(2011, 1, 1), end),
        span(2, date(2012, 1, 1), end),
        span(3, date(2020, 1, 1), end),
    ]
    window, covering = mf.common_window(spans, years=10)
    assert window.start == date(2016, 1, 1)
    assert window.asked_years == 10
    assert covering == [1, 2]
    assert window.too_short == 1


def test_a_window_nobody_covers_is_reported_as_no_window_rather_than_guessed():
    spans = [span(1, date(2019, 1, 1), date(2026, 1, 1))]
    window, covering = mf.common_window(spans, years=15)
    assert not window.ok
    assert covering == []
    assert window.too_short == 1


def test_no_eligible_fund_at_all_gives_no_window():
    window, covering = mf.common_window([span(1, date(2025, 1, 1), date(2026, 1, 1))])
    assert not window.ok
    assert covering == []


def test_slicing_keeps_only_the_window_and_nothing_either_side():
    series = steady(date(2015, 1, 1), 2000, 0.0005)
    part = mf.slice_series(series, date(2018, 1, 1), date(2019, 1, 1))
    assert part.start == date(2018, 1, 1)
    assert part.end == date(2019, 1, 1)
    assert len(part) == 366


# --- the whole point: a late starter stops winning on its start date ------------------------


def test_a_late_starting_fund_in_a_calm_stretch_no_longer_beats_an_older_one():
    """The fund that avoided the crash wins on its own history and loses on the shared one."""
    # Both funds live through exactly the same market from 2018 on, and the older one does
    # very slightly better in it. The only other difference is that the older one was also
    # there for a 40% crash in 2015-17, which is the whole of its apparent disadvantage.
    pre, post = 1095, 1900
    old_navs = []
    for i in range(pre):
        if i < 300:  # a quiet climb to 110
            old_navs.append(100.0 + 10.0 * i / 300)
        elif i < 400:  # then 40% off, which is the fall the newcomer never saw
            old_navs.append(110.0 * (1.0 - 0.40 * (i - 300) / 100))
        else:  # and a long grind back to where it started
            old_navs.append(66.0 + 44.0 * (i - 400) / (pre - 1 - 400))
    for i in range(post):
        old_navs.append(_calm(old_navs[pre - 1], i, rate=0.00030))
    young_navs = [_calm(100.0, i, rate=0.00028) for i in range(post)]

    old = daily(date(2015, 1, 1), old_navs)
    young = daily(date(2018, 1, 1), young_navs)

    def stats_of(code: int, series: mf.Series, years=(1, 3, 5)) -> mf.FundStats:
        return mf.fund_stats(
            code,
            f"fund {code}",
            "equity",
            zip(series.dates, series.navs, strict=True),
            years=years,
            levels=(),
        )

    own = mf.rank_lumpsum([stats_of(1, old), stats_of(2, young)])
    assert own[0].stats.scheme_code == 2, "on its own history the newcomer looks safer"

    window, covering = mf.common_window(
        [mf.Span(1, old.start, old.end), mf.Span(2, young.start, young.end)]
    )
    assert covering == [1, 2]
    assert window.start == young.start  # the newcomer sets the window
    shared = mf.rank_common(
        [
            stats_of(code, mf.slice_series(series, window.start, window.end), years=(1,))
            for code, series in ((1, old), (2, young))
        ]
    )
    # Over the one window they share, the older fund's crash is outside it and the comparison
    # is on the same market: the newcomer no longer wins on the luck of its start date.
    assert shared[0].stats.scheme_code == 1


def test_the_common_measures_are_missing_rather_than_invented_when_a_window_is_flat():
    flat = daily(date(2020, 1, 1), [100.0] * 2000)
    stats = mf.fund_stats(
        1, "flat", "equity", zip(flat.dates, flat.navs, strict=True), years=(1,), levels=()
    )
    measures = mf.common_measures(stats)
    assert measures["return_per_fall"] is None  # it never fell, so there is no ratio
    assert measures["cagr"] == pytest.approx(0.0, abs=1e-9)
    assert measures["worst_1y"] == pytest.approx(0.0, abs=1e-9)


def test_a_tiny_fall_cannot_make_the_ratio_explode():
    barely = mf.FundStats(
        scheme_code=1,
        scheme_name="barely",
        category="hybrid",
        start=date(2015, 1, 1),
        end=date(2026, 1, 1),
        observations=100,
        years=11.0,
        cagr_since_start=0.07,
        volatility=0.02,
        max_drawdown=mf.Drawdown(depth=0.0001),
    )
    # 7% divided by 0.01% would be 700. The floor keeps it to 7% / 1%.
    assert mf.common_measures(barely)["return_per_fall"] == pytest.approx(7.0)
    assert mf.lumpsum_measures(barely)["return_per_fall"] == pytest.approx(7.0)


def test_the_shared_ranking_is_still_independent_of_the_order_funds_arrive_in():
    start = date(2016, 1, 1)
    funds = []
    for code, rate in ((1, 0.0004), (2, 0.0003), (3, 0.00035)):
        series = crashing(start, 2200, at=400, depth=0.25 + code * 0.02, back_by=1000)
        series = daily(start, [n * (1 + rate) ** i for i, n in enumerate(series.navs)])
        funds.append(
            mf.fund_stats(
                code,
                f"fund {code}",
                "equity",
                zip(series.dates, series.navs, strict=True),
                years=(1,),
                levels=(),
            )
        )
    forward = [r.stats.scheme_code for r in mf.rank_common(funds)]
    backward = [r.stats.scheme_code for r in mf.rank_common(list(reversed(funds)))]
    assert forward == backward


# --- the market reference and the survived-a-fall flag --------------------------------------


def test_the_reference_averages_returns_so_a_fund_joining_does_not_put_a_step_in_it():
    builder = mf.Reference()
    builder.add(steady(date(2020, 1, 1), 400, 0.001))
    builder.add(steady(date(2020, 1, 1), 400, 0.001))
    builder.add(steady(date(2020, 6, 1), 200, 0.001, first=5_000.0))  # a far bigger NAV
    index = builder.build()
    # Every fund rises 0.1% a day, so the index must too - the newcomer's size is irrelevant.
    moves = {round(index.navs[i] / index.navs[i - 1] - 1.0, 9) for i in range(1, len(index))}
    assert moves == {0.001}


def test_the_reference_waits_until_enough_funds_are_reporting_together():
    builder = mf.Reference()
    builder.add(steady(date(2020, 1, 1), 400, 0.001))
    assert len(builder.build()) == 0  # one fund is not a market
    builder.add(steady(date(2020, 1, 1), 400, 0.001))
    builder.add(steady(date(2020, 1, 1), 400, 0.001))
    assert len(builder.build()) > 0


def test_an_impossible_daily_move_is_left_out_of_the_reference():
    """One bad NAV would otherwise put a crash into the market every fund is judged against."""
    broken = daily(date(2020, 1, 1), [100.0, 100.0, 1.0, 100.0, 100.0])
    builder = mf.Reference()
    for _ in range(3):
        builder.add(broken)
    index = builder.build()
    assert mf.reference_falls(index) == []


def test_a_fund_that_was_there_through_the_fall_counts_as_having_survived_it():
    falls = [
        mf.Episode(
            peak_date=date(2020, 1, 20),
            peak_nav=100.0,
            reached_on=date(2020, 3, 1),
            depth=0.35,
            recovered_on=date(2020, 11, 10),
            trough_date=date(2020, 3, 23),
        )
    ]
    assert mf.falls_lived_through(date(2015, 1, 1), date(2026, 1, 1), falls) == 1
    assert mf.fall_flag(1) == mf.SURVIVED


def test_a_fund_that_began_after_the_fall_is_untested_not_merely_unlucky():
    falls = [
        mf.Episode(
            peak_date=date(2020, 1, 20),
            peak_nav=100.0,
            reached_on=date(2020, 3, 1),
            depth=0.35,
            recovered_on=date(2020, 11, 10),
            trough_date=date(2020, 3, 23),
        )
    ]
    assert mf.falls_lived_through(date(2021, 1, 1), date(2026, 1, 1), falls) == 0
    assert mf.fall_flag(0) == mf.UNTESTED


def test_joining_halfway_down_does_not_count_as_having_been_through_it():
    """A fund whose record starts mid-crash begins at a price that had already fallen."""
    falls = [
        mf.Episode(
            peak_date=date(2020, 1, 20),
            peak_nav=100.0,
            reached_on=date(2020, 3, 1),
            depth=0.35,
            recovered_on=date(2020, 11, 10),
            trough_date=date(2020, 3, 23),
        )
    ]
    assert mf.falls_lived_through(date(2020, 3, 10), date(2026, 1, 1), falls) == 0


def test_a_fund_that_stopped_before_the_bottom_did_not_live_through_it_either():
    falls = [
        mf.Episode(
            peak_date=date(2020, 1, 20),
            peak_nav=100.0,
            reached_on=date(2020, 3, 1),
            depth=0.35,
            recovered_on=None,
            trough_date=date(2020, 3, 23),
        )
    ]
    assert mf.falls_lived_through(date(2015, 1, 1), date(2020, 3, 5), falls) == 0


def test_a_real_fall_in_a_reference_is_found_with_its_peak_and_its_bottom():
    index = crashing(date(2018, 1, 1), 1600, at=400, depth=0.35, back_by=1100)
    (fall,) = mf.reference_falls(index)
    assert fall.depth > mf.REFERENCE_FALL
    # The crash begins on day 400, so day 400 is still the peak and day 500 the bottom.
    assert fall.peak_date == date(2018, 1, 1) + timedelta(days=400)
    assert fall.trough_date == date(2018, 1, 1) + timedelta(days=500)
    assert fall.recovered_on is not None


def test_the_untested_note_says_it_is_not_a_fault_and_not_a_prediction():
    note = mf.UNTESTED_NOTE.casefold()
    assert "not a fault" in note
    assert "not a prediction" in note
    assert f"{mf.REFERENCE_FALL:.0%}" in mf.UNTESTED_NOTE


# --- windows that do not overlap -------------------------------------------------------------


def test_independent_windows_start_where_the_previous_one_ended():
    series = steady(date(2010, 1, 1), 365 * 10 + 3, 0.0001)
    starts = mf.independent_starts(series.dates, 3)
    assert len(starts) == 3  # ten years holds three three-year stretches
    picked = [series.dates[i] for i in starts]
    assert picked[0] == date(2010, 1, 1)
    assert picked[1] == date(2013, 1, 1)
    assert picked[2] == date(2016, 1, 1)


def test_a_history_shorter_than_one_window_has_no_independent_windows():
    series = steady(date(2020, 1, 1), 300, 0.0001)
    assert mf.independent_starts(series.dates, 3) == []


def test_the_overlapping_count_is_far_larger_than_the_independent_one():
    series = steady(date(2010, 1, 1), 365 * 10 + 3, 0.0003)
    overlapping = mf.rolling_returns(series, 3)
    independent = mf.rolling_returns(series, 3, overlapping=False)
    assert overlapping.count > 2000
    assert independent.count == 3
    # Same steady fund, so both agree on the answer; only the number of tries differs.
    assert overlapping.median == pytest.approx(independent.median, abs=1e-6)


def test_the_independent_spread_is_the_one_that_counts_each_stretch_once():
    # Three distinct years: up 20%, down 20%, up 10%. One non-overlapping window each.
    navs = []
    level = 100.0
    for factor in (1.20, 0.80, 1.10):
        for day in range(365):
            navs.append(level * factor ** (day / 365))
        level *= factor
    navs.extend([level] * 5)  # a few flat days, so the third window has somewhere to end
    series = daily(date(2010, 1, 1), navs)
    independent = mf.rolling_returns(series, 1, overlapping=False)
    assert independent.count == 3
    assert independent.worst == pytest.approx(-0.20, abs=0.01)
    assert independent.best == pytest.approx(0.20, abs=0.01)


def test_the_stats_engine_reports_both_kinds_of_window():
    series = steady(date(2012, 1, 1), 365 * 8, 0.0003)
    stats = mf.fund_stats(
        1,
        "f",
        "equity",
        zip(series.dates, series.navs, strict=True),
        years=(1, 3),
        levels=(),
    )
    assert stats.rolling[3].count > stats.rolling_independent[3].count > 0
    assert stats.rolling_independent[3].count == 2


def test_a_sip_can_be_run_on_windows_that_do_not_overlap():
    flat = daily(date(2010, 1, 1), [100.0] * (365 * 9))
    overlapping = mf.sip_windows([(1_000.0, flat)], 2)
    independent = mf.sip_windows([(1_000.0, flat)], 2, step_months=24)
    assert overlapping.windows > independent.windows > 1
    assert independent.windows == 4  # nine years holds four two-year stretches
    assert independent.multiple.worst == pytest.approx(1.0)


def test_month_starts_can_skip_months_without_losing_the_day_of_the_month():
    months = mf.month_starts(date(2020, 1, 15), date(2022, 1, 20), day=15, step=12)
    assert months == [date(2020, 1, 15), date(2021, 1, 15), date(2022, 1, 15)]


def test_the_overlap_note_refuses_to_call_either_spread_the_safe_one():
    note = mf.OVERLAP_NOTE.casefold()
    assert "not a hundred tries" in note
    assert "narrower of the two spreads is not the safer one" in note


# --- what the report still has to say ------------------------------------------------------


def test_the_window_report_says_who_was_left_out_and_why():
    window = mf.CommonWindow(
        start=date(2016, 1, 1), end=date(2026, 1, 1), funds=10, stale=2, too_short=5
    )
    text = "\n".join(mf_cli.window_lines(window, measured=30))
    assert "2016-01-01 to 2026-01-01" in text
    assert "stale" in text
    assert "do not cover this window" in text
    assert "under 5 years of NAV" in text  # 30 - 10 - 2 - 5


def test_a_report_with_no_shared_window_says_so_instead_of_ranking_anyway():
    window = mf.CommonWindow(start=None, end=None, too_short=4, asked_years=15)
    text = "\n".join(mf_cli.window_lines(window, measured=20))
    assert "No shared window" in text
    assert "the last 15 years" in text


def test_the_common_period_report_still_carries_the_disclaimer_and_the_survivorship_note():
    footer = "\n".join(mf_cli.report_footer())
    assert mf.SURVIVORSHIP_NOTE in footer
    assert "not investment advice" in footer
    note = "\n".join(mf_cli.history_note())
    assert mf.GRADE_NOTE[mf.TOO_NEW] in note


def test_every_common_measure_has_a_note_a_report_can_print():
    assert set(mf.COMMON_MEASURES) == set(mf.COMMON_MEASURE_NOTE)
    assert all(mf.COMMON_MEASURE_NOTE[name] for name in mf.COMMON_MEASURES)


def test_the_common_years_option_is_available_on_the_ranking_command():
    parser = mf_cli.build_parser()
    args = parser.parse_args(
        ["lumpsum", "--amount", "100000", "--risk", "high", "--common-years", "10"]
    )
    assert args.common_years == 10
    assert parser.parse_args(["lumpsum", "--amount", "1", "--risk", "high"]).common_years is None
