"""Mutual funds: the AMFI NAV files, the scheme universe and the "how sure" stats engine.

Everything here is EVIDENCE about what has happened. Nothing predicts a return, ranks a fund as
"good", or promises that a fall will be recovered. Every output carries DISCLAIMER.

The two official AMFI files have DIFFERENT column orders, so both are parsed by their own
header line rather than by position (verified live 2026-11-26):

  NAVAll.txt      Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;
                  Plan;Option;Net Asset Value;Date
  history report  Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
                  ISIN Div Reinvestment;Net Asset Value;Date

Both are semicolon-separated, with blank lines, an AMC name on its own line, and a category
heading such as ``Open Ended Schemes(Equity Scheme - Large Cap Fund)``. The heading is the
only place the category appears, so the parser carries it down the rows beneath it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

DISCLAIMER = (
    "Past performance is not indicative of future results; information, not investment advice."
)

# --- the scheme universe ------------------------------------------------------------------
# AMFI writes the same category two ways ("Equity Scheme - Large Cap Fund" and "Equity
# Schemes - Large Cap Fund"), so matching is on the part before the dash, case-folded.
CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("index funds", "index"),
    ("equity scheme", "equity"),
    ("equity schemes", "equity"),
    ("hybrid scheme", "hybrid"),
    ("hybrid schemes", "hybrid"),
)
# An index fund sits under "Index Funds - Equity Funds" or, for some AMCs, under
# "Other Scheme - Index Funds". It is a rule rather than a manager's choice, so it is its own
# group and the name check below catches the ones AMFI files elsewhere.
INDEX_NAME = re.compile(r"\b(index|nifty|sensex|bse\s*\d|etf)\b", re.IGNORECASE)

HEADING = re.compile(r"^\s*(Open|Close|Interval)\s+Ended\s+Schemes?\s*\(\s*(?P<cat>.+?)\s*\)\s*$")
SCHEME_TYPE = {"open": "open", "close": "close", "interval": "interval"}

UNIVERSE_CATEGORIES = ("equity", "hybrid", "index")


def classify(amfi_category: str, scheme_name: str = "") -> str:
    """AMFI's heading (and, for index funds filed elsewhere, the name) to one of four groups."""
    head = amfi_category.split("-", 1)[0].strip().casefold()
    tail = amfi_category.split("-", 1)[1].strip().casefold() if "-" in amfi_category else ""
    group = next((g for key, g in CATEGORY_RULES if head == key), None)
    if group is None:
        # "Other Scheme - Index Funds" and the bare legacy headings ("Growth", "ELSS").
        if "index" in tail or "index" in head:
            return "index"
        if head in {"growth", "elss"}:
            return "equity"
        return "other"
    if group == "equity" and INDEX_NAME.search(scheme_name):
        return "index"
    if group == "index" and "debt" in tail:
        return "other"  # a debt index fund is not an equity-like holding
    return group


def read_plan(value: str, scheme_name: str = "") -> str:
    text = f"{value} {scheme_name}".casefold()
    if "direct" in text:
        return "direct"
    if "regular" in text:
        return "regular"
    return "unknown"


def read_option(value: str, scheme_name: str = "") -> str:
    """Growth vs IDCW (the old "dividend" option, renamed by SEBI in 2021).

    The Option column is empty for many older schemes, so the name is the fallback: AMFI puts
    "- Growth" or "- IDCW"/"- Dividend" in it.
    """
    text = f"{value} {scheme_name}".casefold()
    if "idcw" in text or "dividend" in text or "payout" in text or "reinvest" in text:
        return "idcw"
    if "growth" in text:
        return "growth"
    return "unknown"


@dataclass(frozen=True, slots=True)
class Scheme:
    scheme_code: int
    scheme_name: str
    amc: str
    amfi_category: str
    category: str
    scheme_type: str
    plan: str
    option: str
    isin_growth: str | None
    isin_reinvest: str | None


@dataclass(frozen=True, slots=True)
class NavRow:
    scheme_code: int
    nav_date: date
    nav: float


def _isin(value: str) -> str | None:
    value = value.strip()
    return value if value and value != "-" else None


def _nav(value: str) -> float | None:
    """AMFI writes N.A., '-' or an empty field on a day a scheme did not publish."""
    value = value.strip()
    if not value or value in {"-", "N.A.", "NA", "N.A"}:
        return None
    try:
        nav = float(value.replace(",", ""))
    except ValueError:
        return None
    return nav if nav > 0 and math.isfinite(nav) else None


def _nav_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _field(parts: Sequence[str], cols: dict[str, int], key: str) -> str:
    """One column of a split data line, or "" when this layout does not have it."""
    i = cols.get(key)
    return parts[i].strip() if i is not None and i < len(parts) else ""


def _columns(header: str) -> dict[str, int]:
    """Map a header line to column positions, so either AMFI layout parses correctly."""
    out: dict[str, int] = {}
    for i, raw in enumerate(header.split(";")):
        name = raw.strip().casefold()
        if "scheme code" in name:
            out["code"] = i
        elif "scheme name" in name or "nav name" in name:
            out["name"] = i
        elif name == "plan":
            out["plan"] = i
        elif name == "option":
            out["option"] = i
        elif "reinvestment" in name:
            out["isin_reinvest"] = i
        elif "isin" in name:
            out["isin_growth"] = i
        elif "net asset value" in name:
            out["nav"] = i
        elif name == "date":
            out["date"] = i
    missing = {"code", "name", "nav", "date"} - out.keys()
    if missing:
        raise ValueError(f"AMFI header is missing {sorted(missing)}: {header[:120]!r}")
    return out


def parse_nav_file(lines: Iterable[str]) -> Iterator[tuple[Scheme, NavRow]]:
    """Yield (scheme, nav row) for every data line of a NAVAll.txt or a history report.

    The same scheme appears once per date in a history report, so the scheme is yielded again
    with each row; the loader keeps the last one it sees.
    """
    cols: dict[str, int] | None = None
    category = ""
    scheme_type = "open"
    amc = ""
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if cols is None:
            if "scheme code" in line.casefold():
                cols = _columns(line)
            continue
        heading = HEADING.match(line)
        if heading:
            category = heading.group("cat")
            scheme_type = SCHEME_TYPE[heading.group(1).casefold()]
            amc = ""  # the AMC name follows its category heading
            continue
        parts = line.split(";")
        code_text = parts[cols["code"]].strip() if len(parts) > cols["code"] else ""
        if len(parts) < 4 or not code_text.isdigit():
            # Not a data row: it is the AMC name, which AMFI writes on a line of its own.
            if ";" not in line:
                amc = line.strip()
            continue

        nav = _nav(_field(parts, cols, "nav"))
        nav_date = _nav_date(_field(parts, cols, "date"))
        if nav is None or nav_date is None:
            continue
        name = _field(parts, cols, "name")
        scheme = Scheme(
            scheme_code=int(code_text),
            scheme_name=name,
            amc=amc or "unknown",
            amfi_category=category or "unknown",
            category=classify(category, name),
            scheme_type=scheme_type,
            plan=read_plan(_field(parts, cols, "plan"), name),
            option=read_option(_field(parts, cols, "option"), name),
            isin_growth=_isin(_field(parts, cols, "isin_growth")),
            isin_reinvest=_isin(_field(parts, cols, "isin_reinvest")),
        )
        yield scheme, NavRow(scheme.scheme_code, nav_date, nav)


def in_universe(scheme: Scheme) -> bool:
    """Open-ended equity, hybrid or index funds, growth option: the owner's scope (S6a)."""
    return (
        scheme.scheme_type == "open"
        and scheme.category in UNIVERSE_CATEGORIES
        and scheme.option == "growth"
    )


# --- the "how sure" stats engine ----------------------------------------------------------
# Everything below works on one fund's NAV series: dates ascending, no duplicates. The window
# lengths are in CALENDAR days, because a rolling 3-year return means three calendar years to
# an investor; the series itself is only on trading days, so each window end is the last NAV
# on or before the target date.

TRADING_DAYS_A_YEAR = 252
ROLLING_YEARS: tuple[int, ...] = (1, 3, 5, 7, 10)
DRAWDOWN_LEVELS: tuple[float, ...] = (0.10, 0.20, 0.30)


@dataclass(frozen=True, slots=True)
class Series:
    """One fund's NAV history, sorted and deduplicated."""

    dates: tuple[date, ...]
    navs: tuple[float, ...]

    @classmethod
    def build(cls, rows: Iterable[tuple[date, float]]) -> Series:
        best: dict[date, float] = {}
        for d, nav in rows:
            if nav is not None and nav > 0:
                best[d] = float(nav)
        order = sorted(best)
        return cls(tuple(order), tuple(best[d] for d in order))

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def start(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def end(self) -> date | None:
        return self.dates[-1] if self.dates else None

    def years(self) -> float:
        if len(self) < 2:
            return 0.0
        return (self.dates[-1] - self.dates[0]).days / 365.25


def _index_on_or_before(dates: Sequence[date], target: date) -> int | None:
    """The position of the last date on or before ``target`` (binary search)."""
    lo, hi = 0, len(dates) - 1
    if hi < 0 or dates[0] > target:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if dates[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def cagr(start_nav: float, end_nav: float, years: float) -> float | None:
    """Compound annual growth rate as a fraction (0.12 = 12% a year)."""
    if years <= 0 or start_nav <= 0 or end_nav <= 0:
        return None
    return (end_nav / start_nav) ** (1.0 / years) - 1.0


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


@dataclass(frozen=True, slots=True)
class Distribution:
    """What a set of observations actually did: the spread, not a single number."""

    count: int
    worst: float | None = None
    p10: float | None = None
    median: float | None = None
    p90: float | None = None
    best: float | None = None
    mean: float | None = None

    @classmethod
    def of(cls, values: Sequence[float]) -> Distribution:
        if not values:
            return cls(count=0)
        s = sorted(values)
        return cls(
            count=len(s),
            worst=s[0],
            p10=percentile(s, 0.10),
            median=percentile(s, 0.50),
            p90=percentile(s, 0.90),
            best=s[-1],
            mean=sum(s) / len(s),
        )


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile of an ALREADY SORTED sequence."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def rolling_returns(series: Series, years: int) -> Distribution:
    """Annualised return of every ``years``-long window, one per trading day as a start.

    A window's end is the last NAV on or before start + ``years`` calendar years, so a fund
    that stopped reporting does not get a free extension.
    """
    out: list[float] = []
    for i, start_date in enumerate(series.dates):
        target = _add_years(start_date, years)
        if target > series.dates[-1]:
            break
        j = _index_on_or_before(series.dates, target)
        if j is None or j <= i:
            continue
        actual_years = (series.dates[j] - start_date).days / 365.25
        value = cagr(series.navs[i], series.navs[j], actual_years)
        if value is not None:
            out.append(value)
    return Distribution.of(out)


def annualised_volatility(series: Series) -> float | None:
    """Standard deviation of daily log returns, annualised by sqrt(252)."""
    if len(series) < 3:
        return None
    rets = [
        math.log(series.navs[i] / series.navs[i - 1])
        for i in range(1, len(series))
        if series.navs[i - 1] > 0 and series.navs[i] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_A_YEAR)


@dataclass(frozen=True, slots=True)
class Drawdown:
    depth: float  # 0.42 = the NAV fell 42% below its peak
    peak_date: date | None = None
    trough_date: date | None = None
    recovered_on: date | None = None

    @property
    def recovery_days(self) -> int | None:
        if self.recovered_on is None or self.peak_date is None:
            return None
        return (self.recovered_on - self.peak_date).days


def max_drawdown(series: Series) -> Drawdown:
    """The deepest fall from any peak, and whether it has been recovered."""
    if len(series) < 2:
        return Drawdown(depth=0.0)
    peak = series.navs[0]
    peak_i = 0
    worst = 0.0
    worst_peak_i = 0
    worst_trough_i = 0
    for i in range(1, len(series)):
        nav = series.navs[i]
        if nav > peak:
            peak, peak_i = nav, i
            continue
        depth = 1.0 - nav / peak
        if depth > worst:
            worst, worst_peak_i, worst_trough_i = depth, peak_i, i
    if worst == 0.0:
        return Drawdown(depth=0.0)
    peak_nav = series.navs[worst_peak_i]
    recovered = next(
        (
            series.dates[k]
            for k in range(worst_trough_i + 1, len(series))
            if series.navs[k] >= peak_nav
        ),
        None,
    )
    return Drawdown(
        depth=worst,
        peak_date=series.dates[worst_peak_i],
        trough_date=series.dates[worst_trough_i],
        recovered_on=recovered,
    )


@dataclass(frozen=True, slots=True)
class FallAndRecovery:
    """How a fund behaved after it fell ``level`` below a peak.

    ``episodes`` counts the separate falls; ``recovered`` counts the ones that got back to
    the SAME peak. ``days`` is the distribution of how long that took, over the recovered
    episodes only — the ones still under water are reported separately and never averaged in,
    because that is exactly how a recovery statistic flatters itself.
    """

    level: float
    episodes: int
    recovered: int
    days: Distribution
    still_down: int
    worst_depth: float | None = None

    @property
    def recovery_rate(self) -> float | None:
        return self.recovered / self.episodes if self.episodes else None


def fall_and_recovery(series: Series, level: float) -> FallAndRecovery:
    """Episodes where the NAV fell at least ``level`` from a running peak.

    An episode opens the first day the fall reaches ``level``, and closes when the NAV gets
    back to that peak. A new episode cannot start until the previous one has closed, so a
    long slide counts once rather than once a day.
    """
    if len(series) < 2 or not 0 < level < 1:
        return FallAndRecovery(level, 0, 0, Distribution.of([]), 0)
    peak = series.navs[0]
    peak_date = series.dates[0]
    open_peak: float | None = None
    open_peak_date: date | None = None
    depth_in_episode = 0.0
    episodes = recovered = still_down = 0
    days: list[float] = []
    depths: list[float] = []
    for i in range(1, len(series)):
        nav, d = series.navs[i], series.dates[i]
        if open_peak is None:
            if nav > peak:
                peak, peak_date = nav, d
            elif 1.0 - nav / peak >= level:
                open_peak, open_peak_date = peak, peak_date
                depth_in_episode = 1.0 - nav / peak
                episodes += 1
        else:
            depth_in_episode = max(depth_in_episode, 1.0 - nav / open_peak)
            if nav >= open_peak:
                recovered += 1
                days.append(float((d - open_peak_date).days))  # type: ignore[operator]
                depths.append(depth_in_episode)
                peak, peak_date = nav, d
                open_peak = open_peak_date = None
    if open_peak is not None:
        still_down = 1
        depths.append(depth_in_episode)
    return FallAndRecovery(
        level=level,
        episodes=episodes,
        recovered=recovered,
        days=Distribution.of(days),
        still_down=still_down,
        worst_depth=max(depths) if depths else None,
    )


@dataclass(frozen=True, slots=True)
class FundStats:
    """Everything S6a measures about ONE fund. No prediction, no score, no recommendation."""

    scheme_code: int
    scheme_name: str
    category: str
    start: date | None
    end: date | None
    observations: int
    years: float
    cagr_since_start: float | None
    volatility: float | None
    max_drawdown: Drawdown
    rolling: dict[int, Distribution] = field(default_factory=dict)
    recovery: dict[float, FallAndRecovery] = field(default_factory=dict)

    @property
    def worst_1y(self) -> float | None:
        d = self.rolling.get(1)
        return d.worst if d else None

    @property
    def worst_3y(self) -> float | None:
        d = self.rolling.get(3)
        return d.worst if d else None


def fund_stats(
    scheme_code: int,
    scheme_name: str,
    category: str,
    rows: Iterable[tuple[date, float]],
    *,
    years: Sequence[int] = ROLLING_YEARS,
    levels: Sequence[float] = DRAWDOWN_LEVELS,
) -> FundStats:
    series = Series.build(rows)
    span = series.years()
    return FundStats(
        scheme_code=scheme_code,
        scheme_name=scheme_name,
        category=category,
        start=series.start,
        end=series.end,
        observations=len(series),
        years=span,
        cagr_since_start=(
            cagr(series.navs[0], series.navs[-1], span) if len(series) >= 2 and span > 0 else None
        ),
        volatility=annualised_volatility(series),
        max_drawdown=max_drawdown(series),
        rolling={y: rolling_returns(series, y) for y in years},
        recovery={lv: fall_and_recovery(series, lv) for lv in levels},
    )


# --- ranking within a category ------------------------------------------------------------
SURVIVORSHIP_NOTE = (
    "Survivorship bias: AMFI's files list the schemes that exist today, so funds that were "
    "merged away or wound up are missing and these statistics are flattered by their absence."
)


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Share of ``values`` at or below ``value``, 0..1. Ties count as half, so the ranking of
    a set of identical numbers is 0.5 rather than 1.0 and does not depend on their order."""
    if not values:
        raise ValueError("no values")
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / len(values)


def rank_within_category(
    stats: Sequence[FundStats], key: str = "cagr_since_start"
) -> dict[int, float]:
    """Percentile of every fund against the OTHER funds in its own category.

    Deterministic: the result depends only on the values, never on the input order (ties get
    the same percentile). Funds whose value is missing are left out of the result entirely
    rather than ranked last, which would be a judgement the data does not support.
    """
    by_category: dict[str, list[float]] = {}
    for s in stats:
        value = getattr(s, key)
        if value is not None:
            by_category.setdefault(s.category, []).append(float(value))
    out: dict[int, float] = {}
    for s in stats:
        value = getattr(s, key)
        if value is None:
            continue
        out[s.scheme_code] = percentile_rank(by_category[s.category], float(value))
    return out


if __name__ == "__main__":  # pragma: no cover - `python -m market_intel.mf fetch|stats`
    import sys

    from market_intel.mf_cli import main

    sys.exit(main())
