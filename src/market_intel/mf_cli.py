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
import sys
import time
from collections.abc import Callable, Sequence
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
    if ten is not None and ten.count:
        out.append(
            f"  10-year windows  worst {pct(ten.worst)}, median {pct(ten.median)}, "
            f"best {pct(ten.best)}   <- the same fund, held ten years"
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


def common_period_caveat(keep: Sequence[mf.FundStats]) -> list[str]:
    """Five years of history is not the SAME five years, and that shows up in the ranking.

    A fund whose NAV begins after a crash has never been through one, so its worst windows
    are only the worst of a short and lucky stretch. The minimum-history rule cannot catch
    that on its own, so the span of start dates is printed and the reader is pointed at each
    fund's own history line.
    """
    starts = [s.start for s in keep if s.start is not None]
    if not starts:
        return []
    return [
        f"  Eligible histories begin between {min(starts)} and {max(starts)}. Five years is "
        "not the SAME five years:",
        "  a fund whose NAV starts after a fall has not been through one, so read each "
        "fund's history line before its score.",
    ]


def run_lumpsum(settings, args: argparse.Namespace) -> int:
    """`mf lumpsum`: which funds history paid best for the falls they put holders through."""
    band = mf.RISK_BANDS[args.risk]
    t0 = time.monotonic()
    plan = None if args.plan == "any" else args.plan
    every, details = measured_stats(settings, category=args.category, plan=plan)
    if not every:
        print("No NAV history held yet. Run `mf fetch`.", file=sys.stderr)
        return 1
    keep, dropped = mf.eligible(every, band)
    too_short = sum(1 for s, _why in dropped if mf.history_grade(s.years) != mf.RANKED)
    print(
        f"LUMPSUM  {rupees(args.amount)} at {band.name} risk"
        + (f", {args.category} funds only" if args.category else "")
        + (f", {args.plan} plans only" if plan else ", every plan")
    )
    print(
        f"{len(every):,} funds measured in {time.monotonic() - t0:.1f} s; "
        f"{len(keep):,} are eligible. Left out: {too_short:,} on history, "
        f"{len(dropped) - too_short:,} outside the {band.name} band."
    )
    if band.max_volatility is not None:
        print(
            f"The {band.name} band means volatility at most {band.max_volatility:.0%} a year "
            f"and a deepest fall of at most {band.max_fall:.0%}."
        )
    else:
        print(f"The {band.name} band sets no ceiling on volatility or on how far a fund fell.")
    if not keep:
        print("\nNo fund clears both gates. Try a wider band or a different category.")
        print("\n".join(history_note() + report_footer()))
        return 0

    ranked = mf.rank_lumpsum(keep)
    print(f"\nRanked on three historical measures, each as a percentile of these {len(keep):,}:")
    for name in mf.LUMPSUM_MEASURES:
        print(f"  {name:<16} {mf.MEASURE_NOTE[name]}")
    print("\n".join(common_period_caveat(keep)))
    print()
    for place, r in enumerate(ranked[: args.top], start=1):
        s = r.stats
        amc = details.get(s.scheme_code)
        print(f"{place}. {s.scheme_name}  [{s.scheme_code}, {s.category}]  score {r.score:.2f}")
        if amc is not None:
            print(f"  fund house       {amc.amc}")
        print(
            "  measures         "
            + " | ".join(
                f"{name} {_measure_text(name, r.measures[name])} (p{r.percentiles[name] * 100:.0f})"
                for name in mf.LUMPSUM_MEASURES
                if name in r.percentiles
            )
        )
        print("\n".join(how_sure_lines(s)))
        print()
    print(
        "The score orders what has already happened. It is not a forecast, not a quality "
        "rating, and says nothing about what any of these funds will do next."
    )
    print("\n".join(history_note() + report_footer()))
    return 0


def _measure_text(name: str, value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if name == "return_per_fall" else pct(value)


def run_sip(settings, args: argparse.Namespace) -> int:
    """`mf sip`: split a monthly amount, then show what such a SIP came to in the past."""
    band = mf.RISK_BANDS[args.risk]
    plan = None if args.plan == "any" else args.plan
    every, details = measured_stats(settings, category=args.category, plan=plan)
    if not every:
        print("No NAV history held yet. Run `mf fetch`.", file=sys.stderr)
        return 1
    eligible_now, _dropped = mf.eligible(every, band)
    # A fund with five years cannot be put in a ten-year plan and then asked how such a plan
    # went: there is no ten-year window to look at. So the horizon raises the history bar.
    need = max(mf.MIN_RANK_YEARS, float(args.years), args.min_history or 0.0)
    keep = [s for s in eligible_now if s.years >= need]
    print(
        f"SIP  {rupees(args.amount)} a month for {args.years} years at {band.name} risk"
        + (f", {args.category} funds only" if args.category else "")
        + (f", {args.plan} plans only" if plan else ", every plan")
    )
    print(
        f"{len(every):,} funds measured; {len(eligible_now):,} eligible, and {len(keep):,} of "
        f"those have the {need:g} years this horizon needs before such a plan can be tested."
    )
    if not keep:
        print(
            f"\nNo eligible fund has {need:g} years of NAV, so there is no window in which a "
            f"{args.years}-year plan can be shown. A shorter --years, a wider --risk band or "
            "another --category would have some. Nothing is guessed in its place."
        )
        print("\n".join(history_note() + report_footer()))
        return 0
    ranked = mf.rank_lumpsum(keep)
    amcs = {code: info.amc for code, info in details.items()}
    picked = mf.diversify(ranked, args.funds, amcs)
    minimums = dict(args.min_sip_for)
    plan = mf.split_sip(picked, args.amount, minimums=minimums, step=mf.SIP_STEP)
    if not plan.slices:
        print("\n" + "\n".join(plan.notes))
        print("\n".join(history_note() + report_footer()))
        return 0

    print(f"\n{len(plan.slices)} funds, at most one per fund house, categories spread first:")
    for slice_ in plan.slices:
        house = details.get(slice_.scheme_code)
        print(
            f"  {rupees(slice_.monthly):>9} a month  {slice_.scheme_name} "
            f"[{slice_.scheme_code}, {slice_.category}]"
            + (f"  - {house.amc}" if house is not None else "")
        )
    for note in plan.notes:
        print(f"  note: {note}")
    print(f"  {mf.MIN_SIP_NOTE}")

    series = named_series(settings, [s.scheme_code for s in plan.slices])
    runs = [(s.monthly, series[s.scheme_code]) for s in plan.slices if s.scheme_code in series]
    outcome = mf.sip_windows(runs, args.years)
    print("\nWhat this plan came to, started on the same day of every month the")
    print("history allows:")
    if outcome.windows == 0:
        print(
            f"  no complete {args.years}-year window: the funds' shared history is shorter "
            "than that. A shorter --years, or funds with more history, would have some."
        )
    else:
        print(
            f"  {outcome.windows} windows, {outcome.first_start} to {outcome.last_start}; "
            f"{rupees(outcome.invested)} paid in over {args.years * 12} instalments each time"
        )
        print(distribution_line("final value", outcome.value, fmt=rupees))
        print(distribution_line("multiple of money", outcome.multiple, fmt=multiple))
        print(distribution_line("annualised (XIRR)", outcome.annualised))
        if outcome.windows < MIN_HONEST_WINDOWS:
            print(
                f"  CAUTION: {outcome.windows} window(s) is too few to call a spread. These "
                "funds' shared history barely covers the horizon; ask for more history with "
                "--min-history, or a shorter --years."
            )
        print(
            "  The windows OVERLAP: starts a month apart share all but one instalment, so "
            "they are not independent tries and the spread understates how different the "
            f"next {args.years} years could be."
        )
        print(
            "  The gap between worst and best is the answer to 'how sure': the same plan, "
            "started in different months, ended that far apart."
        )
    print()
    print("\n".join(mf.stop_rule_lines(years=args.years, goal=args.goal)))
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
