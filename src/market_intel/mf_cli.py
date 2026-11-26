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
from collections.abc import Sequence
from datetime import date

from market_intel import mf, mf_fetch
from market_intel.config import ConfigError, get_settings
from market_intel.db import postgres
from market_intel.logging_setup import configure_logging, get_logger

COMPONENT = "mf"
log = get_logger("market_intel.mf")


def pct(value: float | None, places: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{places}f}%"


def days(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}d"


def distribution_line(label: str, d: mf.Distribution, *, as_percent: bool = True) -> str:
    """One line of a distribution: the spread, never a single headline number."""
    if d.count == 0:
        return f"  {label:<18} no complete window in the history held"
    fmt = pct if as_percent else days
    return (
        f"  {label:<18} n={d.count:>5}  worst {fmt(d.worst):>8}  p10 {fmt(d.p10):>8}  "
        f"median {fmt(d.median):>8}  p90 {fmt(d.p90):>8}  best {fmt(d.best):>8}"
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
                out.append(distribution_line("    days to recover", r.days, as_percent=False))
    return out


def report_footer() -> list[str]:
    return ["", mf.SURVIVORSHIP_NOTE, mf.DISCLAIMER]


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
    return parser


def run_stats(settings, args: argparse.Namespace) -> int:
    with postgres.connect(settings.require("database")) as conn:
        names = mf_fetch.scheme_rows(conn, args.scheme)
        if not names:
            print("No schemes match. Run `mf fetch --master` first.", file=sys.stderr)
            return 1
        t0 = time.monotonic()
        series = mf_fetch.load_series(conn, args.scheme, category=args.category)
    if not series:
        print("No NAV history held for those schemes yet. Run `mf fetch`.", file=sys.stderr)
        return 1
    unknown = ("?", "other")
    every = [mf.fund_stats(code, *names.get(code, unknown), rows) for code, rows in series.items()]
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
        return run_stats(settings, args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
