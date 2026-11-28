"""`python -m market_intel.mf`: download AMFI NAV and show a fund's "how sure" statistics.

Two commands:

    mf fetch   downloads the scheme master and ~15 years of daily NAV (resumable, polite)
    mf stats   computes and prints the statistics for a fund, or for a whole category

Nothing here predicts a return, scores a fund or recommends anything. Every printed report
ends with the disclaimer and the survivorship note, because the statistics are about funds
that still exist, which is not the same as the funds somebody could have bought.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from market_intel import mf, mf_fetch
from market_intel.config import ConfigError, get_settings
from market_intel.db import postgres
from market_intel.logging_setup import configure_logging, get_logger

# Below this many rolling windows there is no spread worth calling one.
MIN_HONEST_WINDOWS = 12

# An AMFI scheme code is a plan AND an option, so one fund appears twice: Direct and
# Regular. Ranking both fills a table with the same fund, and the Regular plan is the
# same portfolio carrying a distributor's commission, so Direct is the default.
PLANS = ("direct", "regular", "any")
PLAN_HELP = "which plan to rank (default direct: the same fund without the commission)"
CALM_HELP = (
    "rank over the longest shared window even when it contains no fall (the default refuses "
    "to, because a rising market says nothing about holding up in a decline)"
)

COMPONENT = "mf"
log = get_logger("market_intel.mf")


def pct(value: float | None, places: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{places}f}%"


def days(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}d"


def distribution_line(
    label: str,
    d: mf.Distribution,
    *,
    as_percent: bool = True,
    fmt: Callable[[float | None], str] | None = None,
    low_is_better: bool = False,
) -> str:
    """One line of a distribution: the spread, never a single headline number.

    ``low_is_better`` is for days to recover, where the SMALLEST number is the good one;
    calling the shortest wait "worst" would read as the opposite of what it means.
    """
    if d.count == 0:
        return f"  {label:<18} no complete window in the history held"
    if fmt is None:
        fmt = pct if as_percent else days
    low, high = ("fastest", "slowest") if low_is_better else ("worst", "best")
    return (
        f"  {label:<18} n={d.count:>5}  {low} {fmt(d.worst):>8}  p10 {fmt(d.p10):>8}  "
        f"median {fmt(d.median):>8}  p90 {fmt(d.p90):>8}  {high} {fmt(d.best):>8}"
    )


def stats_lines(stats: mf.FundStats, *, rank: float | None = None) -> list[str]:
    out = [
        f"{stats.scheme_name}  [{stats.scheme_code}, {stats.category}]",
        f"  history           {stats.start} to {stats.end}  "
        f"({stats.observations:,} NAVs, {stats.years:.1f} years)",
        f"  CAGR since start  {pct(stats.cagr_since_start, 2)}"
        + (f"   (category percentile {rank * 100:.0f})" if rank is not None else ""),
        f"  volatility        {pct(stats.volatility, 1)} a year",
    ]
    dd = stats.max_drawdown
    recovered = (
        f"recovered on {dd.recovered_on} after {dd.recovery_days} days"
        if dd.recovered_on
        else "NOT recovered yet"
    )
    out.append(
        f"  worst fall        {pct(dd.depth, 1)}"
        + (f"  ({dd.peak_date} peak to {dd.trough_date} trough, {recovered})" if dd.depth else "")
    )
    if stats.rolling:
        out.append("  rolling returns a year, over every start date in the history:")
        for years_ in sorted(stats.rolling):
            out.append(distribution_line(f"{years_}-year windows", stats.rolling[years_]))
            apart = stats.rolling_independent.get(years_)
            if apart is not None and apart.count:
                out.append(distribution_line("  of those, independent", apart))
    if stats.recovery:
        out.append("  after a fall from a peak, how often it got back and how long that took:")
        for level in sorted(stats.recovery):
            r = stats.recovery[level]
            if r.episodes == 0:
                out.append(f"  {f'-{level:.0%} falls':<18} never happened in this history")
                continue
            out.append(
                f"  {f'-{level:.0%} falls':<18} {r.episodes} episode(s), "
                f"{r.recovered} recovered, {r.still_down} still down; "
                f"deepest {pct(r.worst_depth, 1)}"
            )
            if r.days.count:
                out.append(
                    distribution_line(
                        "    days to recover", r.days, as_percent=False, low_is_better=True
                    )
                )
    return out


def report_footer() -> list[str]:
    return ["", mf.SURVIVORSHIP_NOTE, mf.DISCLAIMER]


# --- the three decision commands (S6b) ----------------------------------------------------
# All offline: they read the NAV S6a stored and compute on demand. Nothing is written back,
# nothing is predicted, and every report ends with the same footer.


def rupees(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def multiple(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}x"


def measured_stats(
    settings,
    scheme_codes: Sequence[int] | None = None,
    *,
    category: str | None = None,
    plan: str | None = None,
) -> tuple[list[mf.FundStats], dict[int, mf_fetch.SchemeInfo]]:
    """Every fund's statistics, one series at a time so a whole category never sits in RAM.

    ``plan`` matters more than it looks: an AMFI scheme code is a plan AND an option, so the
    Direct and Regular plans of one fund are two codes with almost the same history. Ranked
    together they fill a top-ten list with the same fund twice, and nobody buys both.
    """
    with postgres.connect(settings.require("database")) as conn:
        details = mf_fetch.scheme_details(conn, scheme_codes, category=category, plan=plan)
        if plan and not details:
            return [], {}
        unknown = mf_fetch.SchemeInfo(0, "?", "other", "?", "unknown")
        every = [
            mf.fund_stats(
                code,
                details.get(code, unknown).scheme_name,
                details.get(code, unknown).category,
                rows,
            )
            # With a plan filter the codes are already known, so only those series are read.
            for code, rows in mf_fetch.iter_series(
                conn, sorted(details) if plan else scheme_codes, category=category
            )
        ]
    return every, details


def named_series(settings, scheme_codes: Sequence[int]) -> dict[int, mf.Series]:
    """The NAV of a handful of named funds, for a simulation that needs the series itself."""
    with postgres.connect(settings.require("database")) as conn:
        return {
            code: mf.Series.build(rows)
            for code, rows in mf_fetch.iter_series(conn, list(scheme_codes))
        }


def history_note() -> list[str]:
    """The minimum-history rule, printed wherever a ranking is."""
    return ["", "How much history a fund needs before it is ranked:"] + [
        f"  {grade:<17} {mf.GRADE_NOTE[grade]}"
        for grade in (mf.RANKED, mf.SHORT_HISTORY, mf.TOO_NEW)
    ]


def how_sure_lines(stats: mf.FundStats) -> list[str]:
    """The "how sure" numbers behind one fund, in the order a decision needs them."""
    one, ten = stats.rolling.get(1), stats.rolling.get(10)
    dd = stats.max_drawdown
    back = (
        f"back to that peak in {dd.recovery_days:,} days"
        if dd.recovered_on
        else "NOT back to that peak yet"
    )
    out = [
        f"  history          {stats.start} to {stats.end}  "
        f"({stats.years:.1f} years, {stats.grade})",
        f"  1-year windows   worst {pct(one.worst if one else None)}, "
        f"median {pct(one.median if one else None)}, "
        f"best {pct(one.best if one else None)}   <- the year somebody actually had",
    ]
    independent_1y = stats.rolling_independent.get(1)
    if independent_1y is not None and independent_1y.count:
        out.append(
            f"    independent    worst {pct(independent_1y.worst)}, "
            f"median {pct(independent_1y.median)}, best {pct(independent_1y.best)}   "
            f"<- {independent_1y.count} years that do not overlap"
        )
    if ten is not None and ten.count:
        out.append(
            f"  10-year windows  worst {pct(ten.worst)}, median {pct(ten.median)}, "
            f"best {pct(ten.best)}   <- the same fund, held ten years"
        )
        independent_10y = stats.rolling_independent.get(10)
        if independent_10y is not None and independent_10y.count:
            out.append(
                f"    independent    worst {pct(independent_10y.worst)}, "
                f"median {pct(independent_10y.median)}, best {pct(independent_10y.best)}   "
                f"<- {independent_10y.count} ten-year stretch(es) that do not overlap"
            )
    else:
        out.append("  10-year windows  no complete 10-year window in the history held")
    out.append(f"  deepest fall     {pct(dd.depth)} below a peak, {back}")
    if stats.peak is not None:
        out.append(
            f"  today            {pct(stats.peak.below)} below its highest NAV "
            f"({stats.peak.peak_date}) - a fact, not a reason to buy or to sell"
        )
    return out


@dataclass(frozen=True, slots=True)
class Measured:
    """One pass over the NAV, which is the expensive part, giving everything S6c needs.

    ``full`` is each fund's whole history, for context. ``windowed`` is its slice of the one
    shared window, which is what the headline ranking uses. ``falls`` is the declines found in
    a broad market reference, which is what the survived-a-fall flag is measured against.
    """

    full: dict[int, mf.FundStats]
    windowed: dict[int, mf.FundStats]
    falls: list[mf.Episode]
    reference: mf.Series
    reference_funds: int
    details: dict[int, mf_fetch.SchemeInfo]
    window: mf.CommonWindow
    seconds: float
    # Funds with the five years that the window could not take: on the default path these are
    # the ones that were not there for the fall. They are named on the report, never dropped.
    untested: tuple[int, ...] = ()

    def flag(self, code: int) -> tuple[str, int]:
        """The survived-a-fall flag for one fund, and how many declines it lived through."""
        stats = self.full.get(code)
        if stats is None or stats.start is None or stats.end is None:
            return mf.UNTESTED, 0
        lived = mf.falls_lived_through(stats.start, stats.end, self.falls)
        return mf.fall_flag(lived), lived


def market_reference(settings, *, sample: int = mf.REFERENCE_SAMPLE) -> tuple[mf.Series, int]:
    """A broad equity reference, from the longest-lived equity and index funds held.

    There is no index data in this project, so the yardstick is built out of the funds
    themselves. The sample is the funds with the earliest NAV (ties broken on scheme code), so
    it is deterministic and reaches as far back as the data does; sixty of them give the same
    shape as all of them for the only thing it is used for, which is WHEN the market fell.
    """
    builder = mf.Reference()
    with postgres.connect(settings.require("database")) as conn:
        spans: list[mf.Span] = []
        for name in mf.REFERENCE_CATEGORIES:
            spans.extend(mf_fetch.nav_spans(conn, category=name))
        # Every plan, deliberately: SEBI only created Direct plans in January 2013, so a
        # Direct-only reference cannot see a thing before then and would miss the 2011 fall.
        # Which plan a fund is does not change the shape of the market it moved in.
        spans.sort(key=lambda span: (span.first, span.scheme_code))
        codes = [span.scheme_code for span in spans[:sample]]
        if not codes:
            return mf.Series((), ()), 0
        for _code, rows in mf_fetch.iter_series(conn, codes):
            builder.add(mf.Series.build(rows))
    return builder.build(), len(codes)


def measure_everything(
    settings,
    *,
    category: str | None = None,
    plan: str | None = None,
    common_years: int | None = None,
    require_fall: bool = True,
) -> Measured:
    """Read every relevant series ONCE and get the full history, the window and the flag.

    The window has to be known before the series are read, which is why it comes from a cheap
    aggregate over the dates rather than from the series themselves. Streaming the NAV twice
    would double the only slow part of the command.
    """
    t0 = time.monotonic()
    reference, reference_funds = market_reference(settings)
    # The decline calendar has to exist before the window is chosen, because by default the
    # window is anchored on the most recent decline in it.
    falls = mf.reference_falls(reference)
    unknown = mf_fetch.SchemeInfo(0, "?", "other", "?", "unknown")
    full: dict[int, mf.FundStats] = {}
    windowed: dict[int, mf.FundStats] = {}
    with postgres.connect(settings.require("database")) as conn:
        details = mf_fetch.scheme_details(conn, category=category, plan=plan)
        spans = mf_fetch.nav_spans(conn, category=category, plan=plan)
        window, covering = mf.common_window(
            spans, years=common_years, falls=falls, require_fall=require_fall
        )
        wanted = set(covering)
        eligible, _stale = mf.eligible_spans(spans, mf.MAX_STALE_DAYS, mf.MIN_RANK_YEARS)
        untested = tuple(
            sorted(span.scheme_code for span in eligible if span.scheme_code not in wanted)
        )
        codes = sorted(details) if (plan or category) else None
        for code, rows in mf_fetch.iter_series(conn, codes, category=category):
            info = details.get(code, unknown)
            full[code] = mf.fund_stats(code, info.scheme_name, info.category, rows)
            if code in wanted and window.ok and window.start and window.end:
                part = mf.slice_series(mf.Series.build(rows), window.start, window.end)
                windowed[code] = mf.fund_stats(
                    code,
                    info.scheme_name,
                    info.category,
                    zip(part.dates, part.navs, strict=True),
                    years=(1,),
                    levels=(),
                )
    return Measured(
        full=full,
        windowed=windowed,
        falls=falls,
        reference=reference,
        reference_funds=reference_funds,
        details=details,
        window=window,
        seconds=time.monotonic() - t0,
        untested=untested,
    )


def window_lines(
    window: mf.CommonWindow,
    measured: int,
    falls: Sequence[mf.Episode] = (),
    *,
    default_is_anchored: bool = True,
    escape_flag: str = "--common-years",
) -> list[str]:
    """What the shared window is, how it was chosen, what it cost and what it cannot show.

    The warnings come from :func:`mf.window_evidence`, which both ranking commands share, so
    neither can grow a notice the other lacks. Only two things differ between them and both
    are arguments: whether there is a fall-anchored default to point back to, and which flag
    reaches further back.
    """
    if not window.ok:
        asked = f"the last {window.asked_years} years" if window.asked_years else "any window"
        return [
            f"No shared window: no eligible fund covers {asked}. "
            f"{window.too_short} fund(s) have the five years but not that span."
        ]
    if window.asked_years:
        how = f"the last {window.asked_years} years, as asked"
    elif window.fall is not None:
        how = (
            f"anchored on the {window.fall.depth:.0%} fall of {window.fall.peak_date} to "
            f"{window.fall.trough_date}, so the ranking is over a market that really fell"
        )
    else:
        how = "the longest stretch EVERY eligible fund covers, whatever market it holds"
    out = [
        f"Shared window: {window.start} to {window.end} ({window.years:.1f} years) - {how}.",
        f"  {window.funds:,} of the {measured:,} funds measured are ranked over it.",
    ]
    out.extend(
        f"  {line}"
        for line in mf.window_evidence(window, falls).warnings(
            default_is_anchored=default_is_anchored, escape_flag=escape_flag
        )
    )
    short = measured - window.funds - window.stale - window.too_short
    if short > 0:
        out.append(
            f"  {short:,} fund(s) have under {mf.MIN_RANK_YEARS:g} years of NAV: too little "
            "history to be ranked at all."
        )
    if window.stale:
        out.append(
            f"  {window.stale} fund(s) dropped as stale: no NAV for over "
            f"{mf.MAX_STALE_DAYS} days, so they cannot be bought and would have dragged the "
            "window's end backwards."
        )
    if window.too_short:
        what = (
            "were not reporting through that fall, so they are UNTESTED IN A DOWNTURN"
            if window.fall is not None
            else "do not cover this window"
        )
        out.append(
            f"  {window.too_short:,} fund(s) have {mf.MIN_RANK_YEARS:g} years of NAV but "
            f"{what}: not ranked."
        )
    return out


def untested_lines(got: Measured, show: int = 8) -> list[str]:
    """Name the funds the window could not take, so none of them disappears quietly."""
    if not got.untested:
        return []
    what = (
        "were not there for that fall"
        if got.window.fall is not None
        else "do not cover the shared window"
    )
    out = [
        "",
        f"NOT RANKED - {len(got.untested):,} fund(s) have the "
        f"{mf.MIN_RANK_YEARS:g} years but {what}:",
    ]
    rows = []
    for code in got.untested:
        stats = got.full.get(code)
        if stats is not None:
            rows.append((stats.start or date.max, code, stats.scheme_name))
    rows.sort(reverse=True)  # youngest first: the ones furthest from being comparable
    for first, code, name in rows[:show]:
        out.append(f"  {mf.UNTESTED}: {name} [{code}], NAV from {first}")
    if len(rows) > show:
        out.append(f"  ...and {len(rows) - show:,} more.")
    out.append(
        "  Not a verdict on any of them: their record simply does not cover the same market, "
        "so placing them beside the ranked funds would be a comparison the data cannot make. "
        "`mf stats --scheme <code>` shows what each one's own history does say."
    )
    return out


def reference_lines(got: Measured) -> list[str]:
    """What the market itself did, and whether the shared window contains any of it."""
    if not len(got.reference):
        return ["  not enough funds reporting together to build a reference."]
    out = [
        f"  An equal-weighted index of the {got.reference_funds} longest-lived equity and "
        f"index funds held, {got.reference.start} to {got.reference.end}."
    ]
    if not got.falls:
        out.append(f"  No decline of {mf.REFERENCE_FALL:.0%} or more anywhere in it.")
        return out
    for fall in got.falls:
        back = f"back on {fall.recovered_on}" if fall.recovered_on else "NOT back yet"
        out.append(
            f"    -{fall.depth:.0%}: peak {fall.peak_date}, trough {fall.trough_date}, {back}"
        )
    window = got.window
    inside = [
        f
        for f in got.falls
        if window.start is not None
        and window.end is not None
        and f.peak_date >= window.start
        and (f.trough_date or f.reached_on) <= window.end
    ]
    if inside:
        out.append(f"    {len(inside)} of those fall inside the shared window.")
    else:
        out.append(
            "    NONE of those fall inside the shared window, so the window itself contains "
            "no real decline. Every fund is compared fairly, but on a market that only "
            "rose. The default window is anchored on a fall; this one was asked for."
        )
    return out


def both_windows_line(
    label: str, overlapping: mf.Distribution, independent: mf.Distribution
) -> list[str]:
    """A spread twice: as it is usually reported, and once per stretch of market."""
    out = [distribution_line(f"{label}, overlapping", overlapping)]
    if independent.count:
        out.append(distribution_line(f"{label}, independent", independent))
    return out


def run_lumpsum(settings, args: argparse.Namespace) -> int:
    """`mf lumpsum`: rank the eligible funds over ONE window they all share."""
    band = mf.RISK_BANDS[args.risk]
    plan = None if args.plan == "any" else args.plan
    got = measure_everything(
        settings,
        category=args.category,
        plan=plan,
        common_years=args.common_years,
        require_fall=not args.allow_calm_window,
    )
    if not got.full:
        print("No NAV history held yet. Run `mf fetch`.", file=sys.stderr)
        return 1

    print(
        f"LUMPSUM  {rupees(args.amount)} at {band.name} risk"
        + (f", {args.category} funds only" if args.category else "")
        + (f", {args.plan} plans only" if plan else ", every plan")
    )
    print(f"{len(got.full):,} funds measured in {got.seconds:.1f} s.")
    print("\n".join(window_lines(got.window, len(got.full), got.falls)))
    if not got.window.ok:
        print("\n".join(history_note() + report_footer()))
        return 0

    print("\nWhat the market itself did, so 'survived a fall' means something:")
    print("\n".join(reference_lines(got)))

    pool = [got.full[code] for code in sorted(got.windowed)]
    keep, dropped = mf.eligible(pool, band)
    print(
        f"\n{len(keep):,} of the {len(pool):,} ranked funds are inside the {band.name} band; "
        f"{len(dropped):,} are outside it."
    )
    if band.max_volatility is not None and band.max_fall is not None:
        print(
            f"The {band.name} band means volatility at most {band.max_volatility:.0%} a year "
            f"and a deepest fall of at most {band.max_fall:.0%}, over the fund's whole history."
        )
    else:
        print(f"The {band.name} band sets no ceiling on volatility or on how far a fund fell.")
    if not keep:
        print("\nNo fund clears both gates. Try a wider band or a different category.")
        print("\n".join(history_note() + report_footer()))
        return 0

    ranked = mf.rank_common([got.windowed[s.scheme_code] for s in keep])
    print(
        "\nRanked over the shared window on three measures, each as a percentile "
        f"of these {len(keep):,}:"
    )
    for name in mf.COMMON_MEASURES:
        print(f"  {name:<16} {mf.COMMON_MEASURE_NOTE[name]}")
    print()

    for place, r in enumerate(ranked[: args.top], start=1):
        code = r.stats.scheme_code
        whole = got.full[code]
        flag, lived = got.flag(code)
        print(
            f"{place}. {whole.scheme_name}  [{code}, {whole.category}]  "
            f"score {r.score:.2f}   {flag.upper()}"
            + (f" ({lived} decline(s) lived through)" if lived else "")
        )
        house = got.details.get(code)
        if house is not None:
            print(f"  fund house       {house.amc}")
        print(
            "  shared window    "
            + " | ".join(
                f"{name} {_common_text(name, r.measures[name])} (p{r.percentiles[name] * 100:.0f})"
                for name in mf.COMMON_MEASURES
                if name in r.percentiles
            )
        )
        print("\n".join(how_sure_lines(whole)))
        print()

    print("\n".join(untested_lines(got)))

    own = mf.rank_lumpsum(keep)
    changed = [r.stats.scheme_code for r in own[:3]] != [r.stats.scheme_code for r in ranked[:3]]
    print(
        "Judged on each fund's OWN history instead, the top three would be: "
        + ", ".join(f"{r.stats.scheme_name} [{r.stats.scheme_code}]" for r in own[:3])
        + ("." if changed else " - the same three.")
    )
    if changed:
        print(
            "  That difference is the point of the shared window: a fund whose record starts "
            "in a better year is not a better fund."
        )
    print(
        "\nThe score orders what has already happened over one shared window. It is not a "
        "forecast, not a quality rating, and says nothing about what any of these funds will "
        "do next."
    )
    print(f"\n{mf.UNTESTED_NOTE}")
    print(mf.OVERLAP_NOTE)
    print("\n".join(history_note() + report_footer()))
    return 0


def _common_text(name: str, value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if name == "return_per_fall" else pct(value)


def run_sip(settings, args: argparse.Namespace) -> int:
    """`mf sip`: split a monthly amount, then show what such a SIP came to in the past."""
    band = mf.RISK_BANDS[args.risk]
    plan_filter = None if args.plan == "any" else args.plan
    # A fund with five years cannot be put into a ten-year plan and then asked how such a plan
    # went: there is no ten-year window to look at. The horizon therefore sets the shared
    # window, and only funds covering the whole of it are candidates.
    need = int(math.ceil(max(mf.MIN_RANK_YEARS, float(args.years), args.min_history or 0.0)))
    # The window is the horizon the owner asked for, so there is nothing to anchor: a
    # plan has to be measured over the span it will actually run for.
    got = measure_everything(settings, category=args.category, plan=plan_filter, common_years=need)
    print(
        f"SIP  {rupees(args.amount)} a month for {args.years} years at {band.name} risk"
        + (f", {args.category} funds only" if args.category else "")
        + (f", {args.plan} plans only" if plan_filter else ", every plan")
    )
    if not got.full:
        print("No NAV history held yet. Run `mf fetch`.", file=sys.stderr)
        return 1
    print(f"{len(got.full):,} funds measured in {got.seconds:.1f} s.")
    print(
        "\n".join(
            window_lines(
                got.window,
                len(got.full),
                got.falls,
                default_is_anchored=False,
                escape_flag="--min-history",
            )
        )
    )
    if not got.window.ok:
        print(
            f"\nSo there is no window in which a {args.years}-year plan can be shown. A "
            "shorter --years, a wider --risk band or another --category would have one. "
            "Nothing is guessed in its place."
        )
        print("\n".join(history_note() + report_footer()))
        return 0

    pool = [got.full[code] for code in sorted(got.windowed)]
    keep, _dropped = mf.eligible(pool, band)
    print(f"{len(keep):,} of the {len(pool):,} are inside the {band.name} band and can be picked.")
    if not keep:
        print("\nNo fund clears both gates. Try a wider band or a different category.")
        print("\n".join(history_note() + report_footer()))
        return 0

    ranked = mf.rank_common([got.windowed[s.scheme_code] for s in keep])
    amcs = {code: info.amc for code, info in got.details.items()}
    picked = mf.diversify(ranked, args.funds, amcs)
    sip_plan = mf.split_sip(picked, args.amount, minimums=dict(args.min_sip_for), step=mf.SIP_STEP)
    if not sip_plan.slices:
        print("\n" + "\n".join(sip_plan.notes))
        print("\n".join(history_note() + report_footer()))
        return 0

    print(f"\n{len(sip_plan.slices)} funds, at most one per fund house, categories spread first:")
    for slice_ in sip_plan.slices:
        house = got.details.get(slice_.scheme_code)
        flag, _lived = got.flag(slice_.scheme_code)
        print(
            f"  {rupees(slice_.monthly):>9} a month  {slice_.scheme_name} "
            f"[{slice_.scheme_code}, {slice_.category}]"
            + (f"  - {house.amc}" if house is not None else "")
            + f"  [{flag}]"
        )
    for note in sip_plan.notes:
        print(f"  note: {note}")
    print(f"  {mf.MIN_SIP_NOTE}")

    series = named_series(settings, [s.scheme_code for s in sip_plan.slices])
    runs = [(s.monthly, series[s.scheme_code]) for s in sip_plan.slices if s.scheme_code in series]
    overlapping = mf.sip_windows(runs, args.years)
    independent = mf.sip_windows(runs, args.years, step_months=args.years * 12)
    print("\nWhat this plan came to, started on the same day of every month the")
    print("history allows:")
    if overlapping.windows == 0:
        print(
            f"  no complete {args.years}-year window: the funds' shared history is shorter "
            "than that. A shorter --years, or funds with more history, would have some."
        )
    else:
        print(
            f"  {overlapping.windows} overlapping windows, {overlapping.first_start} to "
            f"{overlapping.last_start}; {rupees(overlapping.invested)} paid in over "
            f"{args.years * 12} instalments each time"
        )
        print(distribution_line("final value", overlapping.value, fmt=rupees))
        print(distribution_line("multiple of money", overlapping.multiple, fmt=multiple))
        print(distribution_line("annualised (XIRR)", overlapping.annualised))
        if overlapping.windows < MIN_HONEST_WINDOWS:
            print(
                f"  CAUTION: {overlapping.windows} window(s) is too few to call a spread. "
                "These funds' shared history barely covers the horizon; ask for more history "
                "with --min-history, or a shorter --years."
            )
        if independent.windows:
            print(
                f"\n  The same plan over {independent.windows} NON-OVERLAPPING window(s), "
                f"{independent.first_start} to {independent.last_start} - each one a stretch "
                "of market the others do not touch:"
            )
            print(distribution_line("final value", independent.value, fmt=rupees))
            print(distribution_line("multiple of money", independent.multiple, fmt=multiple))
            print(distribution_line("annualised (XIRR)", independent.annualised))
        else:
            print(
                f"\n  There is not room for even two {args.years}-year windows side by side "
                "in this history, so there is no independent spread to show. Read the "
                "overlapping one as a single sample, not a range."
            )
        print(f"  {mf.OVERLAP_NOTE}")
        print(
            "  The gap between worst and best is the answer to 'how sure': the same plan, "
            "started at different times, ended that far apart."
        )
    print()
    print("\n".join(mf.stop_rule_lines(years=args.years, goal=args.goal)))
    print(f"\n{mf.UNTESTED_NOTE}")
    print("\n".join(history_note() + report_footer()))
    return 0


def run_keep_or_exit(settings, args: argparse.Namespace) -> int:
    """`mf keep-or-exit`: what history says about falls like this one. The owner decides."""
    held, details = measured_stats(settings, [args.fund])
    if not held:
        print(f"No NAV held for scheme {args.fund}. Run `mf fetch`.", file=sys.stderr)
        return 1
    stats = held[0]
    series = named_series(settings, [args.fund])[args.fund]

    invested = args.invested
    current = args.current
    bought_at: float | None = None
    if current is None:
        bought_at = mf.nav_on_or_before(series, args.buy_date)
        if bought_at is None or series.navs[-1] <= 0:
            print(
                f"The history held starts on {series.start}, after {args.buy_date}. "
                "Pass --current instead.",
                file=sys.stderr,
            )
            return 1
        current = invested / bought_at * series.navs[-1]
    position = mf.Position(invested=invested, current=current)

    print(f"KEEP OR EXIT  {stats.scheme_name}  [{stats.scheme_code}, {stats.category}]")
    house = details.get(args.fund)
    if house is not None:
        print(f"  fund house       {house.amc}")
    print(
        f"\nThe position: {rupees(invested)} put in, worth {rupees(current)} now "
        f"({pct(position.change)})."
    )
    if bought_at is not None:
        print(
            f"  Valued from the NAV: bought at {bought_at:.4f} on or before {args.buy_date}, "
            f"{series.navs[-1]:.4f} on {series.end}."
        )
    if position.change is not None and position.change < 0:
        print(
            f"  It has to rise {pct(position.gain_needed)} from here to be worth what was put in."
        )
    print("\n".join(how_sure_lines(stats)))

    horizon_days = int(args.horizon_years * 365.25)
    level = mf.match_level(stats.peak.below if stats.peak else 0.0, position.change)
    if level is None:
        print(
            "\nThis holding is not far enough below either its cost or the fund's own peak "
            "for a comparable past fall to mean anything (under 5%). There is nothing to "
            "answer here."
        )
        print("\n".join(history_note() + report_footer()))
        return 0

    print(
        f"\nComparable past falls are those of {level:.0%} or more - the deeper of this "
        f"fund's fall from its own peak and the loss on what was put in. Horizon: "
        f"{args.horizon_years:g} years ({horizon_days:,} days)."
    )
    own = mf.recovery_evidence(mf.fall_episodes(series, level), level, horizon_days=horizon_days)
    print(f"\nIn THIS fund, since {stats.start}:")
    print("\n".join(evidence_lines(own)))

    if not args.no_category:
        plan = house.plan if house is not None and house.plan != "unknown" else None
        print(
            f"\nAcross the {stats.category} funds with at least {mf.MIN_RANK_YEARS:g} years"
            + (f", {plan} plans only:" if plan else ":")
        )
        pooled = category_evidence(settings, stats.category, level, horizon_days, plan)
        print("\n".join(evidence_lines(pooled)))

    print("\nKeep or exit - the trade-offs, not a recommendation:")
    for line in mf.KEEP_OR_EXIT_TRADE_OFFS:
        print(f"  - {line}")
    print("\n".join(history_note() + report_footer()))
    return 0


def evidence_lines(ev: mf.RecoveryEvidence) -> list[str]:
    """One block of recovery evidence: counts first, then the spread, never an average."""
    if ev.episodes == 0:
        return [f"  no fall of {ev.level:.0%} or more in the history held."]
    across = f" across {ev.funds:,} funds" if ev.funds > 1 else ""
    out = [
        f"  {ev.episodes:,} fall(s) of {ev.level:.0%} or more{across}; "
        f"{ev.recovered:,} got back to the same peak, {ev.still_down:,} had not when the "
        "history ended."
    ]
    if ev.days.count:
        out.append(
            distribution_line("  days to get back", ev.days, as_percent=False, low_is_better=True)
        )
        out.append(
            f"    within the horizon: {ev.within_horizon:,} of the {ev.recovered:,} that came "
            f"back; longer than it: {ev.beyond_horizon:,}."
        )
    out.append("    The ones still under water are counted on their own and are NOT in those days.")
    return out


def category_evidence(
    settings, category: str, level: float, horizon_days: int, plan: str | None = None
) -> mf.RecoveryEvidence:
    """The same falls, pooled across every fund in the category with enough history.

    One plan only, when the held fund has one: a fund's Direct and Regular codes are nearly
    the same series, and counting both would weigh that one fund twice in the pool.
    """
    episodes: list[mf.Episode] = []
    funds = 0
    with postgres.connect(settings.require("database")) as conn:
        codes = None
        if plan:
            codes = sorted(mf_fetch.scheme_details(conn, category=category, plan=plan))
        for _code, rows in mf_fetch.iter_series(conn, codes, category=category):
            series = mf.Series.build(rows)
            if series.years() < mf.MIN_RANK_YEARS:
                continue
            funds += 1
            episodes.extend(mf.fall_episodes(series, level))
    return mf.recovery_evidence(episodes, level, horizon_days=horizon_days, funds=funds)


def min_sip_pair(text: str) -> tuple[int, float]:
    """``--min-sip-for 118989=1000``: this scheme's own minimum, which AMFI does not publish."""
    code, _, amount = text.partition("=")
    if not amount:
        raise argparse.ArgumentTypeError("use CODE=AMOUNT, for example 118989=1000")
    return int(code), float(amount)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m market_intel.mf")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download the AMFI scheme master and the NAV history")
    fetch.add_argument("--years", type=int, default=mf_fetch.DEFAULT_YEARS)
    fetch.add_argument("--from", dest="start", type=date.fromisoformat)
    fetch.add_argument("--to", dest="end", type=date.fromisoformat)
    fetch.add_argument("--limit", type=int, help="stop after this many chunks (a short run)")
    fetch.add_argument("--master", action="store_true", help="only the scheme master")
    fetch.add_argument("--no-reuse", action="store_true", help="download even if the file is held")
    fetch.add_argument("--dry-run", action="store_true", help="say what would be fetched")

    stats = sub.add_parser("stats", help="the 'how sure' statistics of a fund or a category")
    stats.add_argument("--scheme", type=int, action="append", help="an AMFI scheme code")
    stats.add_argument("--category", choices=mf.UNIVERSE_CATEGORIES)
    stats.add_argument("--top", type=int, default=10, help="how many funds to print")
    stats.add_argument("--min-years", type=float, default=3.0)

    risks = sorted(mf.RISK_BANDS)
    lump = sub.add_parser("lumpsum", help="rank the eligible funds on their own history")
    lump.add_argument("--amount", type=float, required=True, help="the lumpsum, in rupees")
    lump.add_argument("--risk", choices=risks, required=True, help="how much movement is fine")
    lump.add_argument("--category", choices=mf.UNIVERSE_CATEGORIES)
    lump.add_argument("--top", type=int, default=5, help="how many funds to show in full")
    lump.add_argument("--plan", choices=PLANS, default="direct", help=PLAN_HELP)
    lump.add_argument(
        "--common-years",
        type=int,
        help="rank over the last N years instead, and drop the funds that do not cover it",
    )
    lump.add_argument("--allow-calm-window", action="store_true", help=CALM_HELP)

    sip = sub.add_parser("sip", help="split a monthly amount and show what such a SIP did")
    sip.add_argument("--amount", type=float, required=True, help="a month, in rupees")
    sip.add_argument("--years", type=int, required=True, help="the horizon, in years")
    sip.add_argument("--risk", choices=risks, required=True)
    sip.add_argument("--category", choices=mf.UNIVERSE_CATEGORIES)
    sip.add_argument("--funds", type=int, default=3, help="how many funds to spread across")
    sip.add_argument("--plan", choices=PLANS, default="direct", help=PLAN_HELP)
    sip.add_argument("--goal", type=float, help="the amount that ends the plan, in rupees")
    sip.add_argument(
        "--min-history",
        type=float,
        help="demand more NAV history than the horizon needs, for more windows",
    )
    sip.add_argument(
        "--min-sip-for",
        type=min_sip_pair,
        action="append",
        default=[],
        metavar="CODE=AMOUNT",
        help=f"a scheme's own minimum SIP (default {mf.DEFAULT_MIN_SIP:,.0f}; AMFI does not "
        "publish it)",
    )

    keep = sub.add_parser(
        "keep-or-exit", help="what history says about falls like the one a holding is in"
    )
    keep.add_argument("--fund", type=int, required=True, help="the AMFI scheme code held")
    keep.add_argument("--invested", type=float, required=True, help="what was put in")
    keep.add_argument("--current", type=float, help="what it is worth now")
    keep.add_argument(
        "--buy-date", type=date.fromisoformat, help="instead of --current: value it from the NAV"
    )
    keep.add_argument("--horizon-years", type=float, default=5.0, help="how long it can be held")
    keep.add_argument(
        "--no-category", action="store_true", help="skip the category comparison (faster)"
    )
    return parser


def run_stats(settings, args: argparse.Namespace) -> int:
    with postgres.connect(settings.require("database")) as conn:
        names = mf_fetch.scheme_rows(conn, args.scheme)
        if not names:
            print("No schemes match. Run `mf fetch --master` first.", file=sys.stderr)
            return 1
        t0 = time.monotonic()
        unknown = ("?", "other")
        # One fund at a time: a whole category's NAV does not fit comfortably in memory.
        every = [
            mf.fund_stats(code, *names.get(code, unknown), rows)
            for code, rows in mf_fetch.iter_series(conn, args.scheme, category=args.category)
        ]
    if not every:
        print("No NAV history held for those schemes yet. Run `mf fetch`.", file=sys.stderr)
        return 1
    ranks = mf.rank_within_category(every)
    shown = [s for s in every if s.years >= args.min_years]
    shown.sort(key=lambda s: (-(s.cagr_since_start or -9), s.scheme_name))
    print(f"{len(every)} funds measured, {len(shown)} with at least {args.min_years:g} years")
    print(f"computed in {time.monotonic() - t0:.1f} s\n")
    for s in shown[: args.top]:
        print("\n".join(stats_lines(s, rank=ranks.get(s.scheme_code))))
        print()
    print("\n".join(report_footer()))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # A mistake in the arguments is caught before anything is configured or connected to.
    if args.command == "keep-or-exit" and args.current is None and args.buy_date is None:
        print("Pass --current or --buy-date.", file=sys.stderr)
        return 1
    try:
        settings = get_settings()
        configure_logging(settings, COMPONENT)
        if args.command == "fetch":
            result = mf_fetch.run_fetch(
                settings,
                master_only=args.master,
                years=args.years,
                start=args.start,
                end=args.end,
                limit=args.limit,
                reuse=not args.no_reuse,
                dry_run=args.dry_run,
            )
            print(
                f"chunks loaded {result.loaded_chunks}, rows written {result.rows:,}, "
                f"downloaded {result.downloaded / 1e6:.0f} MB"
            )
            if result.stopped:
                print(result.stopped)
            return 0
        if args.command == "lumpsum":
            return run_lumpsum(settings, args)
        if args.command == "sip":
            return run_sip(settings, args)
        if args.command == "keep-or-exit":
            return run_keep_or_exit(settings, args)
        return run_stats(settings, args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
