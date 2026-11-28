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


# --- the default window has to contain a real fall (S6d) ------------------------------------
def a_fall(peak: date, trough: date, depth: float = 0.37) -> mf.Episode:
    return mf.Episode(
        peak_date=peak,
        peak_nav=100.0,
        reached_on=peak + timedelta(days=30),
        depth=depth,
        recovered_on=trough + timedelta(days=200),
        trough_date=trough,
    )


CRASH = a_fall(date(2020, 1, 24), date(2020, 3, 23))
OLDER_CRASH = a_fall(date(2011, 1, 4), date(2011, 12, 20), depth=0.25)
END = date(2026, 11, 25)


def test_the_default_window_contains_the_most_recent_fall():
    spans = [
        span(1, date(2011, 1, 1), END),
        span(2, date(2018, 6, 1), END),
        span(3, date(2021, 11, 15), END),  # after the crash: cannot be ranked over it
    ]
    window, covering = mf.common_window(spans, falls=[OLDER_CRASH, CRASH])

    assert covering == [1, 2]
    assert window.fall is CRASH, "the most recent fall, not the oldest"
    assert window.holds(CRASH)
    assert window.start == date(2018, 6, 1)  # the longest window those two share
    assert window.end == END
    assert window.no_fall_reason is None


def test_a_fund_that_missed_the_fall_is_counted_as_untested_not_discarded():
    spans = [
        span(1, date(2011, 1, 1), END),
        span(2, date(2021, 6, 1), END),
        span(3, date(2021, 11, 15), END),
    ]
    window, covering = mf.common_window(spans, falls=[CRASH])

    assert covering == [1]
    assert window.too_short == 2  # both are named on the report, not dropped
    assert window.holds(CRASH)


def test_the_window_stretches_back_as_far_as_every_ranked_fund_allows():
    """It is the LONGEST window containing the fall, not merely the fall itself."""
    spans = [span(1, date(2011, 1, 1), END), span(2, date(2013, 5, 2), END)]
    window, _covering = mf.common_window(spans, falls=[CRASH])
    assert window.start == date(2013, 5, 2)
    assert window.years > 13


def test_a_fund_that_stopped_before_the_bottom_cannot_anchor_the_window():
    spans = [
        span(1, date(2011, 1, 1), END),
        span(2, date(2011, 1, 1), date(2020, 2, 10)),  # gone before the trough, and stale
    ]
    window, covering = mf.common_window(spans, falls=[CRASH])
    assert covering == [1]
    assert window.stale == 1


def test_the_calm_window_override_gives_back_the_widest_field():
    spans = [
        span(1, date(2011, 1, 1), END),
        span(2, date(2021, 11, 15), END),
    ]
    anchored, ranked_through_fall = mf.common_window(spans, falls=[CRASH])
    calm, ranked_calm = mf.common_window(spans, falls=[CRASH], require_fall=False)

    assert ranked_through_fall == [1]
    assert ranked_calm == [1, 2]  # the wider field is the point of asking
    assert calm.start == date(2021, 11, 15)
    assert calm.fall is None
    assert not calm.holds(CRASH)
    assert anchored.funds < calm.funds


def test_a_named_window_is_honoured_and_says_whether_it_holds_a_fall():
    spans = [span(1, date(2011, 1, 1), END), span(2, date(2013, 1, 1), END)]
    with_fall, _ = mf.common_window(spans, years=10, falls=[CRASH])
    without, _ = mf.common_window(spans, years=3, falls=[CRASH])

    assert with_fall.asked_years == 10
    assert with_fall.fall is CRASH
    assert without.asked_years == 3
    assert without.fall is None  # three years from 2026 does not reach 2020
    assert without.no_fall_reason is None  # it was asked for, not fallen back to


def test_no_fall_anywhere_falls_back_loudly_instead_of_returning_nothing():
    spans = [span(1, date(2011, 1, 1), END), span(2, date(2015, 1, 1), END)]
    window, covering = mf.common_window(spans, falls=[])

    assert covering == [1, 2]  # it still ranks, rather than refusing
    assert window.ok
    assert window.fall is None
    assert window.no_fall_reason is not None
    assert "no decline" in window.no_fall_reason


def test_a_fall_no_eligible_fund_lived_through_falls_back_loudly_too():
    future_crash = a_fall(date(2026, 1, 5), date(2026, 2, 5))
    spans = [span(1, date(2011, 1, 1), date(2025, 12, 1))]
    window, covering = mf.common_window(spans, falls=[future_crash])

    assert covering == [1]
    assert window.no_fall_reason is not None
    assert "was reporting through" in window.no_fall_reason


def test_the_loud_notice_reaches_the_report_rather_than_staying_in_the_object():
    window = mf.CommonWindow(
        start=date(2016, 1, 1),
        end=END,
        funds=10,
        no_fall_reason="no decline of 20% or more exists anywhere in the market reference",
    )
    text = "\n".join(mf_cli.window_lines(window, measured=12))
    assert "!! NO FALL IN THE WINDOW" in text
    assert "only rose" in text
    assert "NOTHING about who holds up in a decline" in text


def test_the_report_says_the_window_was_anchored_on_a_fall_and_names_it():
    window = mf.CommonWindow(start=date(2020, 1, 3), end=END, funds=387, too_short=75, fall=CRASH)
    text = "\n".join(mf_cli.window_lines(window, measured=1128))
    assert "anchored on the 37% fall of 2020-01-24 to 2020-03-23" in text
    assert "UNTESTED IN A DOWNTURN" in text
    assert "75 fund(s)" in text


def test_a_named_window_without_a_fall_is_flagged_on_the_report():
    window = mf.CommonWindow(start=date(2022, 11, 25), end=END, funds=47, asked_years=4)
    text = "\n".join(mf_cli.window_lines(window, measured=370))
    assert "the last 4 years, as asked" in text
    assert "contains no decline" in text


def test_the_funds_left_out_are_named_rather_than_only_counted():
    got = Measured_for([(1, "Old Fund", date(2011, 1, 1)), (2, "New Fund", date(2021, 11, 15))])
    text = "\n".join(mf_cli.untested_lines(got))
    assert "NOT RANKED" in text
    assert "New Fund [2]" in text
    assert "NAV from 2021-11-15" in text
    assert "Not a verdict on any of them" in text
    assert "mf stats --scheme" in text


def test_nothing_is_printed_when_every_eligible_fund_was_ranked():
    got = Measured_for([], untested=())
    assert mf_cli.untested_lines(got) == []


def Measured_for(funds, untested=(2,)):
    """A Measured with just enough in it to render the not-ranked list."""
    full = {}
    for code, name, first in funds:
        full[code] = mf.FundStats(
            scheme_code=code,
            scheme_name=name,
            category="equity",
            start=first,
            end=END,
            observations=100,
            years=(END - first).days / 365.25,
            cagr_since_start=0.12,
            volatility=0.15,
            max_drawdown=mf.Drawdown(depth=0.2),
        )
    return mf_cli.Measured(
        full=full,
        windowed={},
        falls=[CRASH],
        reference=mf.Series((), ()),
        reference_funds=0,
        details={},
        window=mf.CommonWindow(start=date(2020, 1, 3), end=END, funds=1, fall=CRASH),
        seconds=0.0,
        untested=untested,
    )


# --- one downturn-evidence signal, shared by both commands (S6e) ----------------------------
def evidence_for(
    start: date, end: date = END, *, fall: mf.Episode | None = None, falls=(), reason=None
) -> mf.WindowEvidence:
    window = mf.CommonWindow(start=start, end=end, funds=10, fall=fall, no_fall_reason=reason)
    return mf.window_evidence(window, falls)


def test_a_window_holding_a_fall_and_clearing_the_floor_warns_about_nothing():
    evidence = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[OLDER_CRASH, CRASH])
    assert evidence.holds_fall
    assert not evidence.below_floor
    assert not evidence.low_confidence
    assert evidence.warnings() == []


def test_a_window_with_no_fall_says_what_it_can_and_cannot_show():
    evidence = evidence_for(date(2021, 11, 25), falls=[CRASH])
    assert not evidence.holds_fall
    assert evidence.low_confidence
    text = " ".join(evidence.warnings())
    assert "NO FALL IN THIS WINDOW" in text
    assert "contains no decline" in text
    assert "who climbed fastest" in text


def test_the_anchored_default_clause_is_printed_for_lumpsum_and_not_for_sip():
    """The one thing the two commands genuinely differ on."""
    evidence = evidence_for(date(2021, 11, 25), falls=[CRASH])
    lumpsum = " ".join(evidence.warnings(default_is_anchored=True))
    sip = " ".join(evidence.warnings(default_is_anchored=False))

    assert "The default window is anchored on one." in lumpsum
    assert "default window" not in sip
    # Everything else about the two is word for word the same: same sentence, same number,
    # only the marker in front of it differs (S6f).
    assert mf.NO_FALL_SENTENCE in lumpsum
    assert mf.NO_FALL_SENTENCE in sip


def test_each_command_is_told_which_flag_reaches_further_back():
    evidence = evidence_for(date(2021, 11, 25), falls=[CRASH])
    assert "--common-years 7" in " ".join(evidence.warnings())
    assert "--min-history 7" in " ".join(
        evidence.warnings(default_is_anchored=False, escape_flag="--min-history")
    )


def test_a_window_under_the_floor_is_called_low_confidence_and_still_ranked():
    evidence = evidence_for(date(2024, 11, 25), fall=CRASH, falls=[CRASH])
    assert evidence.below_floor
    assert evidence.floor == mf.MIN_SHOW_YEARS == 3.0
    text = " ".join(evidence.warnings())
    assert "LOW CONFIDENCE" in text
    assert "2.0 years" in text
    assert "still shown" in text, "the guard warns, it never refuses"


def test_the_floor_is_the_minimum_history_rule_rather_than_a_new_number():
    assert mf.CONFIDENCE_FLOOR_YEARS == mf.MIN_SHOW_YEARS


def test_a_window_exactly_on_the_floor_is_not_under_it():
    on_it = mf.WindowEvidence(years=3.0, floor=3.0, fall=CRASH)
    under = mf.WindowEvidence(years=2.99, floor=3.0, fall=CRASH)
    assert not on_it.below_floor
    assert under.below_floor


def test_a_short_window_that_does_hold_a_fall_warns_only_about_its_length():
    evidence = evidence_for(date(2024, 11, 25), fall=CRASH, falls=[CRASH])
    text = " ".join(evidence.warnings())
    assert "LOW CONFIDENCE" in text
    assert "NO FALL IN THIS WINDOW" not in text


def test_the_escape_names_the_fall_before_the_one_the_window_is_anchored_on():
    evidence = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[OLDER_CRASH, CRASH])
    assert evidence.previous_fall is OLDER_CRASH
    # 2011-01-04 is just under sixteen years before 2026-11-25.
    assert evidence.escape_years == 16


def test_the_escape_year_count_really_does_reach_that_fall():
    """A number that lands a day short would send the owner round in a circle."""
    evidence = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[OLDER_CRASH, CRASH])
    assert evidence.escape_years is not None
    reached = mf._add_years(END, -evidence.escape_years)
    assert reached <= OLDER_CRASH.peak_date
    # And it is the SMALLEST such number: one year less must fall short.
    assert mf._add_years(END, -(evidence.escape_years - 1)) > OLDER_CRASH.peak_date


def test_no_older_fall_means_no_escape_is_offered_rather_than_a_wrong_one():
    evidence = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[CRASH])
    assert evidence.previous_fall is None
    assert evidence.escape_years is None
    assert not any("pass" in line for line in evidence.warnings())


def test_the_escape_is_not_offered_when_the_window_is_already_sound():
    evidence = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[OLDER_CRASH, CRASH])
    assert evidence.escape_years == 16  # it is known...
    assert evidence.warnings() == []  # ...but there is nothing to escape from


def test_the_loud_fallback_notice_survives_the_move_into_the_shared_signal():
    evidence = evidence_for(
        date(2015, 1, 1), reason="no decline of 20% or more exists anywhere in the reference"
    )
    text = " ".join(evidence.warnings())
    assert "!! NO FALL IN THE WINDOW" in text
    assert "only rose" in text
    assert "NOTHING about who holds up in a decline" in text
    assert "NO FALL IN THIS WINDOW" not in text, "the two notices must not both fire"


def test_a_window_that_could_not_be_built_still_answers_without_raising():
    evidence = mf.window_evidence(mf.CommonWindow(None, None), [CRASH])
    assert evidence.years == 0.0
    assert not evidence.holds_fall


def test_both_commands_print_the_signal_through_the_same_report_helper():
    window = mf.CommonWindow(start=date(2021, 11, 25), end=END, funds=47, asked_years=5)
    lumpsum = "\n".join(mf_cli.window_lines(window, 370, [CRASH]))
    sip = "\n".join(
        mf_cli.window_lines(
            window, 370, [CRASH], default_is_anchored=False, escape_flag="--min-history"
        )
    )
    assert mf.NO_FALL_SENTENCE in lumpsum and mf.NO_FALL_SENTENCE in sip
    assert f"{mf.LOUD} NO FALL IN THIS WINDOW" in lumpsum
    assert f"{mf.CALM} no fall in this window" in sip
    assert "--common-years 7" in lumpsum
    assert "--min-history 7" in sip
    assert "The default window is anchored on one." in lumpsum
    assert "default window" not in sip


def test_a_recent_fall_shortens_the_window_but_the_five_year_rule_floors_it():
    """The collapse the guard exists for cannot happen while ranking needs five years.

    A fall two months ago admits almost every fund, which pushes the window's start to the
    newest eligible fund - but a fund is only eligible with five years of NAV ending now, so
    the window cannot come out shorter than that. The guard is a belt on braces: it fires on
    a window the owner names, and would fire here too if the five-year rule ever moved.
    """
    just_now = a_fall(date(2026, 9, 1), date(2026, 10, 15), depth=0.30)
    spans = [span(1, date(2011, 1, 1), END), span(2, date(2021, 11, 20), END)]

    window, covering = mf.common_window(spans, falls=[just_now])
    assert covering == [1, 2]
    assert window.holds(just_now)
    assert window.years == pytest.approx(5.01, abs=0.02)
    assert not mf.window_evidence(window, [just_now]).below_floor

    # Drop the eligibility rule and the same fall really does collapse the window.
    collapsed, _ = mf.common_window(
        [span(1, date(2011, 1, 1), END), span(3, date(2025, 1, 1), END)],
        falls=[just_now],
        min_years=1.0,
    )
    assert collapsed.years < mf.CONFIDENCE_FLOOR_YEARS
    evidence = mf.window_evidence(collapsed, [just_now])
    assert evidence.below_floor
    assert "LOW CONFIDENCE" in " ".join(evidence.warnings())


# --- the notice's volume follows how surprising the state is (S6f) --------------------------
def no_fall_evidence() -> mf.WindowEvidence:
    """A window holding no fall, above the floor: the only thing that varies is the path."""
    return evidence_for(date(2021, 11, 25), falls=[CRASH])


def test_a_plan_measured_over_its_own_horizon_is_told_calmly():
    """The expected state. Shouting it every run teaches the owner to skip the shouting."""
    lines = no_fall_evidence().warnings(default_is_anchored=False, escape_flag="--min-history")
    assert lines[0].startswith(mf.CALM)
    assert mf.LOUD not in lines[0]
    assert "no fall in this window" in lines[0]


def test_walking_away_from_a_fall_anchored_default_is_still_loud():
    lines = no_fall_evidence().warnings()
    assert lines[0].startswith(mf.LOUD)
    assert "NO FALL IN THIS WINDOW" in lines[0]


def test_the_two_volumes_say_exactly_the_same_thing():
    calm = no_fall_evidence().warnings(default_is_anchored=False)[0]
    loud = no_fall_evidence().warnings()[0]
    assert calm != loud
    assert mf.NO_FALL_SENTENCE in calm
    assert mf.NO_FALL_SENTENCE in loud
    # Strip the marker and the lumpsum-only clause and nothing else is left over.
    assert calm.removeprefix(f"{mf.CALM} no fall in this window: ") == mf.NO_FALL_SENTENCE
    assert loud.removeprefix(f"{mf.LOUD} NO FALL IN THIS WINDOW: ") == (
        f"{mf.NO_FALL_SENTENCE} The default window is anchored on one."
    )


def test_the_escape_hint_is_unchanged_by_the_volume():
    calm = no_fall_evidence().warnings(default_is_anchored=False, escape_flag="--min-history")
    loud = no_fall_evidence().warnings(escape_flag="--min-history")
    assert len(calm) == len(loud) == 2
    assert calm[-1].strip() == loud[-1].strip()
    assert "--min-history 7" in calm[-1]


def test_the_hint_is_indented_under_whichever_notice_it_belongs_to():
    calm = no_fall_evidence().warnings(default_is_anchored=False)[-1]
    loud = no_fall_evidence().warnings()[-1]
    assert len(calm) - len(calm.lstrip()) == len(mf.CALM) + 1
    assert len(loud) - len(loud.lstrip()) == len(mf.LOUD) + 1


def test_the_low_confidence_floor_is_loud_on_both_paths():
    """Out of scope for the volume rule: too little market is too little market."""
    short = evidence_for(date(2024, 11, 25), fall=CRASH, falls=[CRASH])
    for anchored in (True, False):
        floor_line = next(
            line
            for line in short.warnings(default_is_anchored=anchored)
            if "LOW CONFIDENCE" in line
        )
        assert floor_line.startswith(mf.LOUD)
        assert not floor_line.startswith(mf.CALM)


def test_a_calm_notice_and_a_loud_floor_together_keep_their_own_volumes():
    both = evidence_for(date(2024, 11, 25), falls=[CRASH])
    lines = both.warnings(default_is_anchored=False, escape_flag="--min-history")
    assert lines[0].startswith(mf.CALM), "the expected no-fall state stays calm"
    assert lines[1].startswith(mf.LOUD), "the floor does not"
    # The hint sits under the LAST notice, which is the loud one.
    assert len(lines[2]) - len(lines[2].lstrip()) == len(mf.LOUD) + 1


def test_the_loud_fallback_is_never_quietened():
    """The anchored default TRIED and could not find a fall: always a surprise."""
    reason = "no decline of 20% or more exists anywhere in the market reference"
    evidence = evidence_for(date(2015, 1, 1), reason=reason)
    for anchored in (True, False):
        assert evidence.warnings(default_is_anchored=anchored)[0].startswith(mf.LOUD)


def test_a_window_that_is_fine_stays_silent_at_either_volume():
    sound = evidence_for(date(2020, 1, 3), fall=CRASH, falls=[OLDER_CRASH, CRASH])
    assert sound.warnings() == []
    assert sound.warnings(default_is_anchored=False) == []


def test_there_are_exactly_two_volumes_and_they_are_distinguishable():
    assert mf.LOUD != mf.CALM
    assert mf.LOUD == "!!"
    assert mf.CALM == "note:"


def test_the_report_carries_the_calm_notice_through_to_the_sip_command():
    window = mf.CommonWindow(start=date(2021, 11, 25), end=END, funds=47, asked_years=5)
    sip = "\n".join(
        mf_cli.window_lines(
            window, 370, [CRASH], default_is_anchored=False, escape_flag="--min-history"
        )
    )
    assert f"{mf.CALM} no fall in this window" in sip
    assert f"{mf.LOUD} NO FALL" not in sip
    assert "--min-history 7" in sip
