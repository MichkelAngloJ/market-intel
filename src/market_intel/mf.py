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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

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


def rolling_returns(series: Series, years: int, *, overlapping: bool = True) -> Distribution:
    """Annualised return of every ``years``-long window, one per trading day as a start.

    A window's end is the last NAV on or before start + ``years`` calendar years, so a fund
    that stopped reporting does not get a free extension.

    With ``overlapping=False`` each window instead begins where the previous one ended, so no
    stretch of market is counted twice. There are far fewer of them and their spread is wider,
    because that spread is not propped up by the same years appearing over and over.
    """
    starts = range(len(series.dates)) if overlapping else independent_starts(series.dates, years)
    out: list[float] = []
    for i in starts:
        start_date = series.dates[i]
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


@dataclass(frozen=True, slots=True)
class Episode:
    """One fall of at least a given size from a peak, and what happened after it.

    ``depth`` is the deepest the fall got, as a fraction below ``peak_nav``. ``recovered_on``
    is the day the NAV got back to that SAME peak, or None while it is still under water.
    """

    peak_date: date
    peak_nav: float
    reached_on: date  # the day the fall first reached the level
    depth: float
    recovered_on: date | None
    trough_date: date | None = None  # the deepest day of the fall

    @property
    def days(self) -> int | None:
        """Peak to that peak again. None while the fall has not been recovered."""
        if self.recovered_on is None:
            return None
        return (self.recovered_on - self.peak_date).days


def fall_episodes(series: Series, level: float) -> list[Episode]:
    """Every fall of at least ``level`` from a running peak, in order.

    An episode opens the first day the fall reaches ``level`` and closes when the NAV gets
    back to that peak. A new episode cannot start until the previous one has closed, so one
    long slide counts once rather than once a day. At most one episode can be open, so at
    most one can still be under water, and that one is always the last.
    """
    if len(series) < 2 or not 0 < level < 1:
        return []
    peak, peak_date = series.navs[0], series.dates[0]
    # (peak date, peak NAV, the day the fall reached the level) while an episode is open.
    open_ep: tuple[date, float, date] | None = None
    depth = 0.0
    trough: date | None = None
    out: list[Episode] = []
    for i in range(1, len(series)):
        nav, d = series.navs[i], series.dates[i]
        if open_ep is None:
            if nav > peak:
                peak, peak_date = nav, d
            elif 1.0 - nav / peak >= level:
                open_ep = (peak_date, peak, d)
                depth = 1.0 - nav / peak
                trough = d
        else:
            if 1.0 - nav / open_ep[1] > depth:
                depth = 1.0 - nav / open_ep[1]
                trough = d
            if nav >= open_ep[1]:
                out.append(Episode(open_ep[0], open_ep[1], open_ep[2], depth, d, trough))
                peak, peak_date = nav, d
                open_ep, trough = None, None
    if open_ep is not None:
        out.append(Episode(open_ep[0], open_ep[1], open_ep[2], depth, None, trough))
    return out


def fall_and_recovery(series: Series, level: float) -> FallAndRecovery:
    """The episodes of :func:`fall_episodes` counted up: how many, how many came back, and
    the spread of how long that took. The ones still under water are counted separately and
    never averaged into the recovery times."""
    episodes = fall_episodes(series, level)
    days = [float(e.days) for e in episodes if e.days is not None]
    return FallAndRecovery(
        level=level,
        episodes=len(episodes),
        recovered=len(days),
        days=Distribution.of(days),
        still_down=sum(1 for e in episodes if e.recovered_on is None),
        worst_depth=max((e.depth for e in episodes), default=None),
    )


@dataclass(frozen=True, slots=True)
class PeakDistance:
    """Where the latest NAV sits against the highest NAV the fund ever had.

    A fact, not a verdict: being below a peak is not a reason to buy ("it is cheap") or to
    sell ("it is falling"). It is here because a ranking built on a long history says nothing
    about where a fund stands today, and the owner should see both.
    """

    below: float  # 0.08 = the latest NAV is 8% under the highest NAV in the history held
    peak_date: date | None = None
    peak_nav: float | None = None
    latest_nav: float | None = None


def distance_below_peak(series: Series) -> PeakDistance:
    """How far under its own highest NAV a fund is now. Ties take the EARLIEST peak."""
    if not len(series):
        return PeakDistance(0.0)
    best = 0
    for i in range(1, len(series)):
        if series.navs[i] > series.navs[best]:
            best = i
    peak_nav = series.navs[best]
    latest = series.navs[-1]
    return PeakDistance(
        below=max(0.0, 1.0 - latest / peak_nav),
        peak_date=series.dates[best],
        peak_nav=peak_nav,
        latest_nav=latest,
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
    peak: PeakDistance | None = None
    # The same windows again, but NON-OVERLAPPING: each one begins where the last ended, so
    # no stretch of market is counted twice and the spread is not propped up by repetition.
    rolling_independent: dict[int, Distribution] = field(default_factory=dict)

    @property
    def grade(self) -> str:
        """How much history there is, which decides whether this fund can be ranked."""
        return history_grade(self.years)

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
        peak=distance_below_peak(series),
        rolling_independent={y: rolling_returns(series, y, overlapping=False) for y in years},
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


# --- how much history a fund has, before anything is ranked (S6b) -------------------------
# 841 of the 2,255 funds in the universe have under three years of NAV. Two things go wrong
# if they are ranked anyway: there is not enough history to tell a good fund from a lucky
# one, and a fund that happened to launch into a rising market tops the table on nothing but
# its start date. So the rule is explicit, shared by all three decision commands, and shown
# on every output rather than applied quietly.
MIN_RANK_YEARS = 5.0
MIN_SHOW_YEARS = 3.0

RANKED = "ranked"
SHORT_HISTORY = "short history"
TOO_NEW = "too new to judge"

GRADE_NOTE = {
    RANKED: f"at least {MIN_RANK_YEARS:g} years of NAV: ranked",
    SHORT_HISTORY: (
        f"{MIN_SHOW_YEARS:g} to {MIN_RANK_YEARS:g} years of NAV: shown with a flag, never "
        "ranked against funds with a long history"
    ),
    TOO_NEW: (
        f"under {MIN_SHOW_YEARS:g} years of NAV: too new to judge on its own history. "
        "Judging it on anything else needs a model of funds LIKE it, which this project has "
        "not built."
    ),
}


def history_grade(years: float) -> str:
    """Which of the three history groups a fund falls into. The minimum-history rule itself."""
    if years >= MIN_RANK_YEARS:
        return RANKED
    if years >= MIN_SHOW_YEARS:
        return SHORT_HISTORY
    return TOO_NEW


# --- risk comfort, measured rather than labelled ------------------------------------------
@dataclass(frozen=True, slots=True)
class RiskBand:
    """How much movement the owner is willing to live with.

    The ceilings are a CHOICE made here, not an industry standard, and they are written down
    so that "low risk" means the same thing every time it is printed. A fund is inside or
    outside a band because of what its own NAV did, never because of its name or its label.
    """

    name: str
    max_volatility: float | None
    max_fall: float | None

    def outside(self, stats: FundStats) -> tuple[str, ...]:
        """Why this fund sits outside the band, or () when it sits inside."""
        out: list[str] = []
        if self.max_volatility is not None:
            if stats.volatility is None:
                out.append("its volatility cannot be measured")
            elif stats.volatility > self.max_volatility:
                out.append(
                    f"volatility {stats.volatility:.0%} a year is over the "
                    f"{self.max_volatility:.0%} this band allows"
                )
        if self.max_fall is not None and stats.max_drawdown.depth > self.max_fall:
            out.append(
                f"it has fallen {stats.max_drawdown.depth:.0%} below a peak, past the "
                f"{self.max_fall:.0%} this band allows"
            )
        return tuple(out)


RISK_BANDS: dict[str, RiskBand] = {
    "low": RiskBand("low", 0.12, 0.25),
    "medium": RiskBand("medium", 0.18, 0.40),
    "high": RiskBand("high", None, None),
}


# --- the lumpsum ranking ------------------------------------------------------------------
# Three measures, all computed from what the fund did and all "higher is better". None of
# them is a forecast and none is a score of the fund's quality: together they say which funds
# were paid best for the falls they put their holders through.
LUMPSUM_MEASURES: tuple[str, ...] = ("return_per_fall", "median_5y", "worst_3y")

MEASURE_NOTE = {
    "return_per_fall": "return a year since the fund started, per unit of its deepest fall",
    "median_5y": "the middle 5-year window: what a typical five years actually returned",
    "worst_3y": "the WORST 3-year window: the bad case somebody really lived through",
}


def lumpsum_measures(stats: FundStats) -> dict[str, float | None]:
    """The three ranking measures for one fund. A measure it cannot support is None."""
    fall = stats.max_drawdown.depth
    grew = stats.cagr_since_start
    five, three = stats.rolling.get(5), stats.rolling.get(3)
    return {
        # A fund that has never fallen gets no ratio here rather than an infinite one. With
        # five years of real NAV that does not happen; a synthetic series can. A fall smaller
        # than MIN_MEANINGFUL_FALL is treated as that, because dividing by a near-zero
        # denominator makes the ratio say more about the denominator than about the fund.
        "return_per_fall": None
        if grew is None or fall <= 0
        else grew / max(fall, MIN_MEANINGFUL_FALL),
        "median_5y": five.median if five is not None and five.count else None,
        "worst_3y": three.worst if three is not None and three.count else None,
    }


@dataclass(frozen=True, slots=True)
class Ranked:
    stats: FundStats
    score: float  # 0..1, the mean of the percentiles below
    percentiles: dict[str, float]
    measures: dict[str, float | None]


def rank_lumpsum(stats: Sequence[FundStats]) -> list[Ranked]:
    """Rank funds on all three measures at once, each turned into a percentile first.

    The measures are in different units - a ratio and two annual returns - so averaging them
    raw would hand the ordering to whichever one happens to have the widest spread. Each is
    converted to its percentile within this pool first, and the score is the mean of those.

    Deterministic: ties share a percentile and the final sort breaks ties on the scheme code,
    so the ordering depends only on the numbers, never on the order the funds arrived in.
    """
    return rank_on(stats, LUMPSUM_MEASURES, lumpsum_measures)


def eligible(
    stats: Sequence[FundStats], band: RiskBand
) -> tuple[list[FundStats], list[tuple[FundStats, tuple[str, ...]]]]:
    """Split funds into the ones that can be ranked in this band and the ones that cannot.

    Two gates, in this order: enough history (the minimum-history rule), then the risk band.
    The second list carries the reason for every fund left out, so nothing disappears without
    the report being able to say why.
    """
    keep: list[FundStats] = []
    dropped: list[tuple[FundStats, tuple[str, ...]]] = []
    for s in stats:
        grade = history_grade(s.years)
        if grade != RANKED:
            dropped.append((s, (f"{grade} ({s.years:.1f} years of NAV)",)))
            continue
        why = band.outside(s)
        if why:
            dropped.append((s, why))
        else:
            keep.append(s)
    return keep, dropped


# --- the SIP plan -------------------------------------------------------------------------
# AMFI's files carry NAV, not terms: the minimum SIP instalment is NOT in them. It is an
# input with a documented default, and the report says so, because a plan that quietly
# assumes a minimum is a plan the owner cannot act on.
# Below this, a drawdown is noise and the return-per-fall ratio stops meaning anything.
MIN_MEANINGFUL_FALL = 0.01

DEFAULT_MIN_SIP = 500.0
SIP_STEP = 100.0
MIN_SIP_NOTE = (
    "AMFI's NAV files do not carry each scheme's minimum SIP, so it is an input: the default "
    f"of {DEFAULT_MIN_SIP:,.0f} a month is the common industry minimum, not this scheme's. "
    "Check the scheme information document before starting a SIP."
)


@dataclass(frozen=True, slots=True)
class SipSlice:
    scheme_code: int
    scheme_name: str
    category: str
    monthly: float
    minimum: float


@dataclass(frozen=True, slots=True)
class SipPlan:
    slices: tuple[SipSlice, ...]
    monthly: float
    notes: tuple[str, ...] = ()


def diversify(
    ranked: Sequence[Ranked], count: int, amcs: Mapping[int, str] | None = None
) -> list[Ranked]:
    """The best-ranked funds, but not several of the same kind.

    At most one fund per AMC, because one fund house having a bad decade should not take the
    whole plan with it, and every category is used once before any category gets a second
    fund. ``ranked`` is already sorted, so the choice is deterministic.
    """
    picked: list[Ranked] = []
    chosen: set[int] = set()
    used_amc: set[str] = set()
    used_category: set[str] = set()
    houses = amcs or {}
    for spread_first in (True, False):
        for r in ranked:
            if len(picked) >= count:
                break
            if r.stats.scheme_code in chosen:
                continue
            amc = houses.get(r.stats.scheme_code, "")
            if amc and amc in used_amc:
                continue
            if spread_first and r.stats.category in used_category:
                continue
            picked.append(r)
            chosen.add(r.stats.scheme_code)
            used_category.add(r.stats.category)
            if amc:
                used_amc.add(amc)
    return picked


def split_sip(
    picked: Sequence[Ranked],
    amount: float,
    *,
    minimums: Mapping[int, float] | None = None,
    step: float = SIP_STEP,
) -> SipPlan:
    """Split a monthly amount across the chosen funds, respecting each one's minimum.

    An equal share rounded down to a whole ``step``, with whatever is left over going to the
    top-ranked fund so the instalments add up to exactly what the owner said. If the share
    does not clear some fund's minimum, the plan holds one fund fewer and says so: spreading
    an amount so thin that the AMC would reject the mandate is not a plan.
    """
    floors = dict(minimums or {})
    notes: list[str] = []
    chosen = list(picked)
    share = 0.0
    while chosen:
        share = math.floor(amount / len(chosen) / step) * step
        if all(share >= floors.get(r.stats.scheme_code, DEFAULT_MIN_SIP) for r in chosen):
            break
        notes.append(
            f"{amount:,.0f} a month does not clear every minimum across {len(chosen)} funds, "
            f"so the plan holds {len(chosen) - 1}."
        )
        chosen.pop()
    if not chosen:
        return SipPlan(
            (),
            amount,
            (f"{amount:,.0f} a month is below the minimum of every eligible fund.",),
        )
    remainder = amount - share * len(chosen)
    slices = tuple(
        SipSlice(
            scheme_code=r.stats.scheme_code,
            scheme_name=r.stats.scheme_name,
            category=r.stats.category,
            monthly=share + (remainder if i == 0 else 0.0),
            minimum=floors.get(r.stats.scheme_code, DEFAULT_MIN_SIP),
        )
        for i, r in enumerate(chosen)
    )
    return SipPlan(slices, amount, tuple(notes))


def xirr(
    flows: Sequence[tuple[date, float]], *, lo: float = -0.95, hi: float = 10.0
) -> float | None:
    """The annual rate that makes a set of dated cash flows add up to nothing (bisection).

    Money going out is negative, money coming back positive. This is what a SIP ACTUALLY
    earned over a window that has already happened; it says nothing about the next one.
    """
    if len(flows) < 2:
        return None
    base = flows[0][0]

    def npv(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** ((when - base).days / 365.25) for when, amount in flows)

    low, high = lo, hi
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:  # no sign change: no rate in this range makes the flows balance
        return None
    for _ in range(100):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


def month_starts(first: date, last: date, day: int = 1, step: int = 1) -> list[date]:
    """The same day of every ``step``-th month from ``first`` to ``last``, inclusive.

    A month too short for the day (the 31st of February) uses its 28th, which is what a
    mandate does in practice. ``step`` is 1 for instalments; a SIP's own start dates use a
    whole horizon as the step when the windows have to be independent.
    """
    out: list[date] = []
    year, month = first.year, first.month
    while True:
        try:
            when = date(year, month, day)
        except ValueError:
            when = date(year, month, 28)
        if when > last:
            return out
        if when >= first:
            out.append(when)
        month += step
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1


def nav_on_or_before(series: Series, when: date) -> float | None:
    """The NAV an instalment made on ``when`` would have been bought at."""
    i = _index_on_or_before(series.dates, when)
    return None if i is None else series.navs[i]


@dataclass(frozen=True, slots=True)
class SipOutcome:
    """What a SIP like this one came to, over every window the history allows.

    A spread, never a single number: the same plan started three months apart ended very
    differently, and that difference is the honest answer to "how sure is this?".
    """

    windows: int
    invested: float
    value: Distribution
    multiple: Distribution
    annualised: Distribution
    first_start: date | None = None
    last_start: date | None = None


def sip_run(
    plan: Sequence[tuple[float, Series]], start: date, years: int
) -> tuple[float, float, float | None] | None:
    """One SIP: invest the same amounts every month for ``years``, then value what is held.

    Returns (invested, final value, annualised rate), or None when any fund is missing NAV
    for any part of the window. A window only some of the funds covered would flatter the
    plan, so it is dropped rather than patched.
    """
    end = _add_years(start, years)
    for _monthly, series in plan:
        if series.start is None or series.end is None:
            return None
        if series.start > start or series.end < end:
            return None
    units = [0.0] * len(plan)
    per_month = sum(monthly for monthly, _s in plan)
    flows: list[tuple[date, float]] = []
    invested = 0.0
    for when in month_starts(start, end - timedelta(days=1), start.day):
        for k, (monthly, series) in enumerate(plan):
            nav = nav_on_or_before(series, when)
            if nav is None:
                return None
            units[k] += monthly / nav
        invested += per_month
        flows.append((when, -per_month))
    if not flows:
        return None
    value = 0.0
    for k, (_monthly, series) in enumerate(plan):
        nav = nav_on_or_before(series, end)
        if nav is None:
            return None
        value += units[k] * nav
    flows.append((end, value))
    return invested, value, xirr(flows)


def sip_windows(
    plan: Sequence[tuple[float, Series]], years: int, *, step_months: int = 1
) -> SipOutcome:
    """Run the same SIP from the same day of every ``step_months``-th month history allows.

    ``step_months`` of ``years * 12`` makes the windows NON-OVERLAPPING: each one uses a
    stretch of market the others do not touch, so the spread is the honest width rather than
    the same decade counted a hundred times.
    """
    empty = Distribution.of([])
    if not plan or years <= 0:
        return SipOutcome(0, 0.0, empty, empty, empty)
    starts = [s.start for _m, s in plan if s.start is not None]
    ends = [s.end for _m, s in plan if s.end is not None]
    if len(starts) != len(plan) or len(ends) != len(plan):
        return SipOutcome(0, 0.0, empty, empty, empty)
    values: list[float] = []
    multiples: list[float] = []
    rates: list[float] = []
    used: list[date] = []
    invested = 0.0
    for when in month_starts(max(starts), _add_years(min(ends), -years), step=step_months):
        run = sip_run(plan, when, years)
        if run is None:
            continue
        invested, value, rate = run
        values.append(value)
        multiples.append(value / invested if invested > 0 else 0.0)
        if rate is not None:
            rates.append(rate)
        used.append(when)
    return SipOutcome(
        windows=len(used),
        invested=invested,
        value=Distribution.of(values),
        multiple=Distribution.of(multiples),
        annualised=Distribution.of(rates),
        first_start=used[0] if used else None,
        last_start=used[-1] if used else None,
    )


def stop_rule_lines(*, years: int, goal: float | None, review_months: int = 6) -> list[str]:
    """The plain rule for when the SIP ends. Decided up front, not in the middle of a fall."""
    out = [
        "STOP the SIP when ANY of these is true - decide it now, not during a fall:",
        f"  1. the horizon ends: {years} years from the first instalment.",
        (
            f"  2. the goal is reached: the holding is worth {goal:,.0f} or more."
            if goal is not None
            else "  2. the goal is reached (pass --goal to put a number here)."
        ),
        f"  3. a fund falls out of the better half of its category (percentile under 50 on "
        f"`mf lumpsum`) at {review_months} monthly reviews in a row. Stop THAT fund's "
        "instalment and move it to the next eligible fund, not the whole plan.",
        "A fall in the market is NOT on this list. The windows above already include the "
        "falls; stopping inside one is what turns a bad window into a realised loss.",
    ]
    return out


# --- keep or exit -------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Position:
    """What the owner put in and what it is worth now. Their numbers, not the fund's."""

    invested: float
    current: float

    @property
    def change(self) -> float | None:
        """-0.18 = the holding is 18% below what was put into it."""
        return None if self.invested <= 0 else self.current / self.invested - 1.0

    @property
    def gain_needed(self) -> float | None:
        """How far it has to rise from here to be worth what was put in."""
        return None if self.current <= 0 else self.invested / self.current - 1.0


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """What happened after falls at least this deep. History; not a promise about this one."""

    level: float
    funds: int
    episodes: int
    recovered: int
    still_down: int
    days: Distribution
    within_horizon: int
    beyond_horizon: int
    horizon_days: int | None = None

    @property
    def recovery_rate(self) -> float | None:
        return self.recovered / self.episodes if self.episodes else None


def recovery_evidence(
    episodes: Sequence[Episode], level: float, *, horizon_days: int | None, funds: int = 1
) -> RecoveryEvidence:
    """Count falls of this depth, and split the recoveries by the owner's remaining horizon.

    The episodes still under water are counted on their own and never averaged into the
    recovery times, because leaving them out is exactly how a recovery number flatters
    itself.
    """
    days = [float(e.days) for e in episodes if e.days is not None]
    within = sum(1 for d in days if horizon_days is not None and d <= horizon_days)
    return RecoveryEvidence(
        level=level,
        funds=funds,
        episodes=len(episodes),
        recovered=len(days),
        still_down=sum(1 for e in episodes if e.recovered_on is None),
        days=Distribution.of(days),
        within_horizon=within if horizon_days is not None else 0,
        beyond_horizon=(len(days) - within) if horizon_days is not None else 0,
        horizon_days=horizon_days,
    )


def match_level(fall_from_peak: float, position_change: float | None) -> float | None:
    """Which depth of past fall is comparable to where this holding stands now.

    The deeper of the fund's own fall from its peak and the owner's loss: the first is what
    the episode statistics measure, the second is what the owner is actually living with.
    Under 5% there is nothing distinctive to match, and the answer is None.
    """
    loss = -position_change if position_change is not None and position_change < 0 else 0.0
    level = max(fall_from_peak, loss)
    return round(level, 4) if 0.05 <= level < 1.0 else None


KEEP_OR_EXIT_TRADE_OFFS = (
    "Keeping means the money stays exposed to this same fund. The recovery times above are a "
    "SPREAD of what happened before, not a schedule, and some past falls had still not been "
    "recovered when the history ended.",
    "Exiting means the loss stops being on paper and becomes final, and any past fall that "
    "did come back would have been missed. Exit load and capital-gains tax are real costs "
    "and are NOT modelled here.",
    "Neither column is a recommendation. Nothing here says this fund will recover; the "
    "decision is the owner's.",
)


# --- comparing funds of different ages on the same market (S6c) ---------------------------
# S6b ranked every fund on its OWN history, and five years is not the same five years: a fund
# whose NAV begins in mid-2020 starts at the bottom of a crash, so its worst case is the worst
# of a lucky stretch and it can top a table without ever having been through a fall. The
# headline ranking is therefore computed over ONE window that every ranked fund covers, and
# each fund is flagged by whether its own history lived through a decline at all.
MAX_STALE_DAYS = 30
REFERENCE_FALL = 0.20
MIN_REFERENCE_FUNDS = 3
# The decline calendar is built from a sample of the longest-lived equity and index funds,
# not from every fund and not per category. Two reasons. An equal-weighted index of sixty
# funds has the same shape as one of eleven hundred, so the rest is work for nothing. And the
# question the flag answers is "did this fund exist while the MARKET fell", which a category's
# own index cannot answer: the hybrid index never fell 20% even in 2020, so judging hybrid
# funds against it marks every one of them untested, including the ones that lived through it.
REFERENCE_SAMPLE = 60
REFERENCE_CATEGORIES = ("equity", "index")
# A daily move further than this is a data error, not a market: one bad NAV would otherwise
# put a fake crash into the reference every fund is then judged against.
MAX_PLAUSIBLE_DAILY_MOVE = 0.5


@dataclass(frozen=True, slots=True)
class Span:
    """How much NAV is held for one fund. Cheap: it comes from a GROUP BY, not the series."""

    scheme_code: int
    first: date
    last: date

    @property
    def years(self) -> float:
        return (self.last - self.first).days / 365.25


@dataclass(frozen=True, slots=True)
class CommonWindow:
    """The one stretch of market every ranked fund is measured over."""

    start: date | None
    end: date | None
    funds: int = 0
    stale: int = 0
    too_short: int = 0
    asked_years: int | None = None
    # The decline the window was anchored on, when it was anchored on one at all.
    fall: Episode | None = None
    # Set when a fall was wanted and could not be had. It is printed loudly, because a
    # ranking over a market that only rose is a different thing from one that includes a
    # decline, and the difference must never be silent.
    no_fall_reason: str | None = None

    @property
    def years(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start).days / 365.25

    @property
    def ok(self) -> bool:
        return self.start is not None and self.end is not None and self.funds > 0

    def holds(self, fall: Episode) -> bool:
        """Whether this window covers a decline from its peak to its bottom."""
        if self.start is None or self.end is None:
            return False
        bottom = fall.trough_date or fall.reached_on
        return self.start <= fall.peak_date and self.end >= bottom


def eligible_spans(
    spans: Sequence[Span], stale_days: int, min_years: float
) -> tuple[list[Span], int]:
    """The funds a window may be built from: still reporting, and old enough to be ranked."""
    if not spans:
        return [], 0
    newest = max(s.last for s in spans)
    fresh = [s for s in spans if (newest - s.last).days <= stale_days]
    return [s for s in fresh if s.years >= min_years], len(spans) - len(fresh)


def _window_over(
    candidates: Sequence[Span], eligible: Sequence[Span], stale: int, **extra: Any
) -> tuple[CommonWindow, list[int]]:
    """Build the window every one of ``candidates`` covers, and say who was left out."""
    if not candidates:
        return CommonWindow(None, None, stale=stale, too_short=len(eligible), **extra), []
    start = max(s.first for s in candidates)
    end = min(s.last for s in candidates)
    if start >= end:
        return CommonWindow(None, None, stale=stale, too_short=len(eligible), **extra), []
    return (
        CommonWindow(
            start=start,
            end=end,
            funds=len(candidates),
            stale=stale,
            too_short=len(eligible) - len(candidates),
            **extra,
        ),
        sorted(s.scheme_code for s in candidates),
    )


def common_window(
    spans: Sequence[Span],
    *,
    years: int | None = None,
    falls: Sequence[Episode] = (),
    require_fall: bool = True,
    stale_days: int = MAX_STALE_DAYS,
    min_years: float = MIN_RANK_YEARS,
) -> tuple[CommonWindow, list[int]]:
    """Choose the one window the ranking is computed over, and say who covers it.

    Eligibility is the minimum-history rule ALONE, deliberately: the window is then the same
    whatever risk band the owner asks for, so the ranking does not silently move because a
    ceiling changed. A fund that has stopped reporting is dropped first - one dead fund would
    otherwise drag the window's end back by years and take every live fund's recent history
    with it.

    Three ways to choose it, in this order:

    ``years``
        The last ``years`` of market, exactly as asked. Only funds covering all of it are
        ranked. The result reports whether it happens to contain a fall; it is not required
        to, because the owner named the period.

    ``require_fall`` (the DEFAULT, S6d)
        The window must contain a real decline, so the ranking reflects how funds behaved in
        one rather than who climbed fastest in a rising market. The MOST RECENT decline in
        ``falls`` is used - a fixed number of years would drift away from it as time passes -
        and the window is then the longest one covered by every fund that was already
        reporting at that decline's peak and still reporting at its bottom. Funds too young
        for it are not ranked; they are the "untested in a downturn" ones, counted and named
        on the report rather than quietly dropped.

    neither
        The longest trailing window every eligible fund covers, whatever market it holds.
        This is what ``require_fall=False`` asks for, and what the two paths above fall back
        to when there is no usable decline - with ``no_fall_reason`` set, so the report can
        say loudly that the ranking is on a market that only rose.
    """
    eligible, stale = eligible_spans(spans, stale_days, min_years)
    if not eligible:
        return CommonWindow(None, None, stale=stale, asked_years=years), []

    if years is not None:
        end = min(s.last for s in eligible)
        start = _add_years(end, -years)
        covering = [s for s in eligible if s.first <= start]
        if not covering or start >= end:
            return CommonWindow(
                None, None, stale=stale, too_short=len(eligible), asked_years=years
            ), []
        # Built with the REQUESTED start, not the widest one the funds would allow: the fall
        # check has to run against the window actually used, or it answers the wrong question.
        window = CommonWindow(
            start=start,
            end=end,
            funds=len(covering),
            stale=stale,
            too_short=len(eligible) - len(covering),
            asked_years=years,
        )
        held = next((f for f in falls if window.holds(f)), None)
        return replace(window, fall=held), sorted(s.scheme_code for s in covering)

    if require_fall:
        if not falls:
            reason = (
                f"no decline of {REFERENCE_FALL:.0%} or more exists anywhere in the market "
                "reference, so no window could be anchored on one"
            )
        else:
            latest = max(falls, key=lambda f: f.peak_date)
            bottom = latest.trough_date or latest.reached_on
            through = [s for s in eligible if s.first <= latest.peak_date and s.last >= bottom]
            window, codes = _window_over(through, eligible, stale, fall=latest)
            if window.ok:
                return window, codes
            reason = (
                f"no fund with {min_years:g} years of NAV was reporting through the "
                f"{latest.depth:.0%} fall of {latest.peak_date}, so no window could be "
                "anchored on it"
            )
        window, codes = _window_over(eligible, eligible, stale, no_fall_reason=reason)
        return window, codes

    return _window_over(eligible, eligible, stale)


def slice_series(series: Series, start: date, end: date) -> Series:
    """The part of a fund's NAV inside a window, so every fund is measured over one market."""
    keep = [(d, nav) for d, nav in zip(series.dates, series.navs, strict=True) if start <= d <= end]
    return Series(tuple(d for d, _n in keep), tuple(n for _d, n in keep))


# The common-period measures are NOT the full-history ones. Over a single shared window a
# plain CAGR is already comparable between funds, which it never is across different spans,
# and the 5-year and 3-year windows the full-history ranking leans on barely exist inside a
# window that is itself about five years long.
COMMON_MEASURES: tuple[str, ...] = ("return_per_fall", "cagr", "worst_1y")

COMMON_MEASURE_NOTE = {
    "return_per_fall": "return a year over the shared window, per unit of its deepest fall",
    "cagr": "return a year over the shared window - the same market for every fund here",
    "worst_1y": "the WORST year inside the shared window: the bad case over that same market",
}


def common_measures(stats: FundStats) -> dict[str, float | None]:
    """The ranking measures for one fund, computed on its slice of the shared window."""
    fall = stats.max_drawdown.depth
    grew = stats.cagr_since_start
    one = stats.rolling.get(1)
    return {
        "return_per_fall": (
            None if grew is None or fall <= 0 else grew / max(fall, MIN_MEANINGFUL_FALL)
        ),
        "cagr": grew,
        "worst_1y": one.worst if one is not None and one.count else None,
    }


def rank_on(
    stats: Sequence[FundStats],
    names: Sequence[str],
    measure_of: Callable[[FundStats], dict[str, float | None]],
) -> list[Ranked]:
    """Rank funds on several measures at once, each turned into a percentile first.

    The measures are in different units, so averaging them raw would hand the ordering to
    whichever happens to have the widest spread. Ties share a percentile and the final sort
    breaks ties on the scheme code, so the ordering depends only on the numbers.
    """
    measured = [(s, measure_of(s)) for s in stats]
    pools: dict[str, list[float]] = {}
    for name in names:
        pools[name] = sorted(v for _s, m in measured if (v := m[name]) is not None)
    out: list[Ranked] = []
    for s, m in measured:
        percentiles: dict[str, float] = {}
        for name in names:
            value = m[name]
            if value is not None and pools[name]:
                percentiles[name] = percentile_rank(pools[name], value)
        score = sum(percentiles.values()) / len(percentiles) if percentiles else 0.0
        out.append(Ranked(stats=s, score=score, percentiles=percentiles, measures=m))
    out.sort(key=lambda r: (-r.score, r.stats.scheme_code))
    return out


def rank_common(stats: Sequence[FundStats]) -> list[Ranked]:
    """The headline ranking: every fund measured over the one window they all share."""
    return rank_on(stats, COMMON_MEASURES, common_measures)


# --- how much downturn evidence a window holds (S6e) --------------------------------------
# S6d grew two separate warnings - one for a window with no fall in it, one for the loud
# fallback - and `mf sip` printed the first of them only because it happened to share the
# helper, with a closing clause ("the default window would have one") that is true of
# `mf lumpsum` and false of `mf sip`. Both commands now ask ONE object what their window can
# and cannot show, and print what it answers. There is no second place for the two to drift.
#
# A window is low confidence for either of two reasons, and they are different:
#   - it holds no decline, so it says who climbed fastest and nothing about who holds up;
#   - it is shorter than the floor below which a ranking is mostly noise.
CONFIDENCE_FLOOR_YEARS = MIN_SHOW_YEARS

# Two volumes, and nothing in between. LOUD is for a state the owner would not expect;
# CALM is for one they would. The WORDS after the marker are the same either way: this is
# about how hard the report knocks, never about what it says.
LOUD = "!!"
CALM = "note:"

NO_FALL_SENTENCE = (
    f"it contains no decline of {REFERENCE_FALL:.0%} or more, so it says who climbed "
    "fastest and nothing about who holds up in a fall."
)


def _years_back_to(end: date, target: date) -> int:
    """The smallest whole N for which ``end`` minus N years is on or before ``target``.

    This is what a ``--common-years N`` has to be to reach back to a given day, and it is
    computed by stepping rather than dividing because the window itself is built with
    calendar arithmetic: a leap year makes a division-based answer land a day short.
    """
    n = max(1, int((end - target).days // 365))
    while _add_years(end, -n) > target:
        n += 1
    return n


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """What one window can and cannot show. The single signal both commands print from."""

    years: float
    floor: float
    fall: Episode | None = None
    previous_fall: Episode | None = None
    escape_years: int | None = None
    no_fall_reason: str | None = None

    @property
    def holds_fall(self) -> bool:
        return self.fall is not None

    @property
    def below_floor(self) -> bool:
        return self.years < self.floor

    @property
    def low_confidence(self) -> bool:
        return self.below_floor or not self.holds_fall

    def warnings(
        self, *, default_is_anchored: bool = True, escape_flag: str = "--common-years"
    ) -> list[str]:
        """The lines to print. Empty when the window holds a fall and clears the floor.

        ``default_is_anchored`` is the one thing the two commands genuinely differ on:
        `mf lumpsum` has a fall-anchored default to point the owner back to, `mf sip` does
        not, because a plan has to be measured over the span it will actually run for.

        It also sets the VOLUME of the no-fall notice, because the same fact is ordinary in
        one place and alarming in the other. A plan measured over its own horizon will hold
        no fall most of the time - shouting about it every run teaches the owner to skip the
        shouting, and the next notice that matters goes with it. Walking away from a
        fall-anchored default that was there for the asking is genuinely unusual, so that one
        stays loud. The words are identical either way; only the marker changes.
        """
        out: list[str] = []
        indent = len(LOUD) + 1
        if self.no_fall_reason:
            # The anchored default TRIED and could not find a fall: always surprising.
            out.append(
                f"{LOUD} NO FALL IN THE WINDOW: {self.no_fall_reason}. This ranking is over "
                "a market that only rose, so it says who climbed fastest and NOTHING about "
                "who holds up in a decline. Read it that way."
            )
        elif not self.holds_fall:
            if default_is_anchored:
                out.append(
                    f"{LOUD} NO FALL IN THIS WINDOW: {NO_FALL_SENTENCE} The default window "
                    "is anchored on one."
                )
            else:
                out.append(f"{CALM} no fall in this window: {NO_FALL_SENTENCE}")
                indent = len(CALM) + 1
        if self.below_floor:
            # The floor warning is loud wherever it fires: too little market is too little
            # market, and nothing about the command asking makes that expected.
            out.append(
                f"{LOUD} LOW CONFIDENCE: this window is {self.years:.1f} years, under the "
                f"{self.floor:g}-year floor. A ranking over that little market is mostly "
                "noise - it is still shown, because a loud ranking beats none, but read it "
                "as a hint and not as evidence."
            )
            indent = len(LOUD) + 1
        if self.escape_years is not None and self.low_confidence:
            reach = self.previous_fall
            where = (
                f"the {reach.depth:.0%} fall of {reach.peak_date}"
                if reach is not None
                else "an older fall"
            )
            # Indented to sit under the notice it belongs to, whichever marker that carried.
            out.append(
                f"{' ' * indent}To rank over a window that reaches {where} instead, pass "
                f"{escape_flag} {self.escape_years}. Fewer funds will have that much "
                "history; the report will say how many."
            )
        return out


def window_evidence(
    window: CommonWindow,
    falls: Sequence[Episode] = (),
    *,
    floor: float = CONFIDENCE_FLOOR_YEARS,
) -> WindowEvidence:
    """Assess one window: does it hold a decline, is it long enough, and what reaches further.

    ``escape_years`` is the ``--common-years`` (or ``--min-history``) value that would reach
    the next qualifying fall further back - named rather than taken, so the owner chooses a
    smaller, older field knowingly instead of having it chosen for them.
    """
    if window.end is None:
        return WindowEvidence(years=0.0, floor=floor, no_fall_reason=window.no_fall_reason)
    earlier = [f for f in falls if window.fall is None or f.peak_date < window.fall.peak_date]
    previous = max(earlier, key=lambda f: f.peak_date) if earlier else None
    escape = _years_back_to(window.end, previous.peak_date) if previous is not None else None
    return WindowEvidence(
        years=window.years,
        floor=floor,
        fall=window.fall,
        previous_fall=previous,
        escape_years=escape,
        no_fall_reason=window.no_fall_reason,
    )


# --- has this fund ever been through a fall? ----------------------------------------------
class Reference:
    """An equal-weighted, daily-rebalanced index of a category, built from the funds held.

    There is no index data in this project, so the reference is made out of the category's own
    NAV: each day's move is the average move of every fund that reported on that day AND the
    day before. Averaging RETURNS rather than levels is what lets funds join and leave without
    putting a step into the series.

    It inherits the survivorship bias of the data it is made from, so it is a yardstick for
    "was there a decline around then", never a benchmark a fund is scored against.
    """

    __slots__ = ("counts", "sums")

    def __init__(self) -> None:
        self.sums: dict[date, float] = {}
        self.counts: dict[date, int] = {}

    def add(self, series: Series) -> None:
        for i in range(1, len(series)):
            if series.navs[i - 1] <= 0:
                continue
            move = series.navs[i] / series.navs[i - 1] - 1.0
            if abs(move) > MAX_PLAUSIBLE_DAILY_MOVE:
                continue
            when = series.dates[i]
            self.sums[when] = self.sums.get(when, 0.0) + move
            self.counts[when] = self.counts.get(when, 0) + 1

    def build(self, *, min_funds: int = MIN_REFERENCE_FUNDS) -> Series:
        """The index itself, starting at 100 on the first day enough funds reported."""
        dates: list[date] = []
        levels: list[float] = []
        level = 100.0
        for when in sorted(self.sums):
            if self.counts[when] < min_funds:
                continue
            level *= 1.0 + self.sums[when] / self.counts[when]
            dates.append(when)
            levels.append(level)
        return Series(tuple(dates), tuple(levels))


def reference_falls(reference: Series, level: float = REFERENCE_FALL) -> list[Episode]:
    """The declines of at least ``level`` in a category reference, peak to trough and back."""
    return fall_episodes(reference, level)


SURVIVED = "survived a fall"
UNTESTED = "untested in a downturn"

UNTESTED_NOTE = (
    "A fund marked 'untested in a downturn' has no decline of "
    f"{REFERENCE_FALL:.0%} or more anywhere in its own history. That is not a fault of the "
    "fund and not a prediction about it: it means its record says nothing at all about how it "
    "behaves when the market falls, so the comparison with a fund that has been through one "
    "is not like for like."
)


def falls_lived_through(first: date, last: date, falls: Sequence[Episode]) -> int:
    """How many of the reference's declines this fund's own history covers.

    A fund counts as having lived through a decline only if it was already reporting at the
    PEAK and still reporting at the trough. Joining halfway down means its record starts at a
    price that had already fallen, which is the flattering case this flag exists to catch.
    """
    count = 0
    for fall in falls:
        bottom = fall.trough_date or fall.reached_on
        if first <= fall.peak_date and last >= bottom:
            count += 1
    return count


def fall_flag(count: int) -> str:
    return SURVIVED if count else UNTESTED


# --- windows that are actually independent ------------------------------------------------
# Rolling windows a day or a month apart share almost all of their history, so a hundred of
# them are nowhere near a hundred tries and their spread reads far more settled than the truth.
# Every spread this module reports therefore comes with the non-overlapping version beside it.
OVERLAP_NOTE = (
    "Read both spreads. Overlapping windows share almost all of their history, so counting "
    "them overstates how much evidence there is - a hundred of them are not a hundred tries. "
    "Non-overlapping windows use each stretch of market exactly once, but there are few of "
    "them and a fall landing across a boundary can be split between two windows and show up "
    "in neither, so the narrower of the two spreads is not the safer one."
)


def independent_starts(dates: Sequence[date], years: int) -> list[int]:
    """Positions of window starts that do not overlap: each begins where the last one ended."""
    out: list[int] = []
    i = 0
    if not dates or years <= 0:
        return out
    while i < len(dates):
        target = _add_years(dates[i], years)
        if target > dates[-1]:
            break
        j = _index_on_or_before(dates, target)
        if j is None or j <= i:
            break
        out.append(i)
        i = j
    return out


if __name__ == "__main__":  # pragma: no cover - `python -m market_intel.mf fetch|stats`
    import sys

    from market_intel.mf_cli import main

    sys.exit(main())
