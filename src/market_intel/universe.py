"""Choosing an intraday universe from the daily candles we already have (sprint S5i-c;
decision 2026-11-26).

On the Nifty 50 nothing beat "no trading": the per-trade gross edge was about 1 basis point
against a round trip of roughly 8.3 bps in charges alone. The binding constraint is the SIZE of
the move, not the holding time, so the next question is whether a more volatile — but still
liquid — universe moves far enough to clear the fee.

The selection uses only daily candles (no new downloads) and is deterministic:

- **liquidity** = the median daily turnover (close x volume) over the look-back window. A floor
  keeps the universe to names where a few basis points of slippage is a realistic assumption;
  picking volatile but thin stocks would simply move the cost from the fee to the spread.
- **volatility** = the median daily range ((high - low) / close) over the same window.
- stocks must already have daily history before the intraday data starts, so the 1-minute
  backfill has a full period to cover.

Then the most volatile ``size`` of the survivors are taken, and the owner's named picks are
always added, whether or not they made the cut. Nothing here downloads anything: it writes
``universe_members`` rows, and ``candles intraday --universe <name>`` does the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

CRORE = 10_000_000.0
DEFAULT_SOURCE = "nifty500"
DEFAULT_LOOKBACK_DAYS = 500  # about two trading years
DEFAULT_MIN_TURNOVER = 25 * CRORE  # median daily turnover, so ~5 bps slippage is plausible
DEFAULT_SIZE = 100
HISTORY_BEFORE = date(2022, 1, 1)  # the first day Upstox serves 1-minute candles

# The owner's named picks (2026-11-26): always in the universe, whatever the screen says.
NAMED_PICKS: tuple[str, ...] = (
    "HINDCOPPER",
    "MAZDOCK",
    "AEGISLOG",
    "PETRONET",
    "HAL",
    "FEDERALBNK",
    "IDFCFIRSTB",
)

SELECT_SQL = """
SELECT d.instrument_id,
       i.symbol,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY d.close * d.volume)
           FILTER (WHERE d.trade_date >= %(start)s) AS turnover,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY (d.high - d.low) / d.close)
           FILTER (WHERE d.trade_date >= %(start)s) AS day_range,
       min(d.trade_date) AS first_day,
       count(*) FILTER (WHERE d.trade_date >= %(start)s) AS days
  FROM candles_daily d
  JOIN instruments i ON i.id = d.instrument_id
  JOIN universe_members u ON u.instrument_id = d.instrument_id AND u.universe = %(source)s
 WHERE i.is_active AND d.close > 0
 GROUP BY d.instrument_id, i.symbol
HAVING count(*) FILTER (WHERE d.trade_date >= %(start)s) > 0
"""


@dataclass(frozen=True)
class Candidate:
    instrument_id: int
    symbol: str
    turnover: float  # rupees a day, median
    day_range: float  # fraction of price, median
    first_day: date
    days: int
    named: bool = False

    @property
    def turnover_crore(self) -> float:
        return self.turnover / CRORE


@dataclass
class UniversePlan:
    name: str
    source: str
    size: int
    min_turnover: float
    lookback_days: int
    considered: int = 0
    liquid: int = 0
    chosen: list[Candidate] = field(default_factory=list)
    missing_picks: list[str] = field(default_factory=list)
    written: int = 0
    dry_run: bool = False

    @property
    def named(self) -> list[Candidate]:
        return [c for c in self.chosen if c.named]

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        ranges = [c.day_range for c in self.chosen]
        turnovers = [c.turnover_crore for c in self.chosen]
        out = [
            f"{prefix}universe {self.name}: {len(self.chosen)} stocks"
            f" (from {self.considered} in {self.source}, {self.liquid} above"
            f" Rs {self.min_turnover / CRORE:,.0f} crore of median daily turnover)",
            f"  median daily range {100 * _median(ranges):.2f}%"
            f" (lowest {100 * min(ranges, default=0):.2f}%,"
            f" highest {100 * max(ranges, default=0):.2f}%)",
            f"  median daily turnover Rs {_median(turnovers):,.0f} crore"
            f" (lowest Rs {min(turnovers, default=0):,.0f} crore)",
            f"  named picks included: {', '.join(c.symbol for c in self.named) or 'none'}",
        ]
        if self.missing_picks:
            out.append(f"  named picks NOT FOUND in instruments: {', '.join(self.missing_picks)}")
        if not self.dry_run:
            out.append(f"  rows written to universe_members: {self.written}")
        return out


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def choose(
    candidates: list[Candidate],
    *,
    size: int = DEFAULT_SIZE,
    min_turnover: float = DEFAULT_MIN_TURNOVER,
    history_before: date = HISTORY_BEFORE,
    picks: tuple[str, ...] = NAMED_PICKS,
) -> tuple[list[Candidate], int]:
    """The liquid, long-enough-history candidates ranked by volatility, plus the named picks.

    Pure and deterministic: ties break by symbol, so the same data always gives the same
    universe. Returns (chosen, how many cleared the liquidity floor).
    """
    wanted = {p.upper() for p in picks}
    eligible = [
        c
        for c in candidates
        if c.turnover >= min_turnover
        and c.first_day < history_before
        and c.symbol.upper() not in wanted
    ]
    liquid = len(eligible)
    ranked = sorted(eligible, key=lambda c: (-c.day_range, c.symbol))[:size]
    # `choose` is what decides a stock is here because it was named, so it sets the flag
    named = sorted(
        (replace(c, named=True) for c in candidates if c.symbol.upper() in wanted),
        key=lambda c: c.symbol,
    )
    chosen = [*named, *ranked]
    return chosen, liquid


def load_candidates(
    conn: Any,
    *,
    source: str = DEFAULT_SOURCE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    picks: tuple[str, ...] = NAMED_PICKS,
) -> tuple[list[Candidate], list[str]]:
    """Every stock of ``source`` with its liquidity and volatility, plus the named picks.

    A named pick is looked up by symbol whether or not it belongs to ``source``, and keeps its
    measured statistics when it has daily history. Returns (candidates, picks not found).
    """
    last = conn.execute("SELECT max(trade_date) FROM candles_daily").fetchone()[0]
    if last is None:
        return [], list(picks)
    start = last - timedelta(days=lookback_days)
    rows = conn.execute(SELECT_SQL, {"source": source, "start": start}).fetchall()
    found = {
        str(symbol).upper(): Candidate(
            instrument_id=int(inst),
            symbol=str(symbol),
            turnover=float(turnover or 0),
            day_range=float(day_range or 0),
            first_day=first_day,
            days=int(days),
        )
        for inst, symbol, turnover, day_range, first_day, days in rows
    }
    missing: list[str] = []
    for pick in picks:
        key = pick.upper()
        if key in found:
            found[key] = Candidate(**{**found[key].__dict__, "named": True})
            continue
        row = conn.execute(
            "SELECT i.id, i.symbol,"
            " percentile_disc(0.5) WITHIN GROUP (ORDER BY d.close * d.volume)"
            "   FILTER (WHERE d.trade_date >= %(start)s),"
            " percentile_disc(0.5) WITHIN GROUP (ORDER BY (d.high - d.low) / d.close)"
            "   FILTER (WHERE d.trade_date >= %(start)s),"
            " min(d.trade_date), count(d.*) FILTER (WHERE d.trade_date >= %(start)s)"
            " FROM instruments i LEFT JOIN candles_daily d"
            " ON d.instrument_id = i.id AND d.close > 0"
            " WHERE upper(i.symbol) = %(symbol)s AND i.segment = 'EQ' AND i.is_active"
            " GROUP BY i.id, i.symbol ORDER BY i.id LIMIT 1",
            {"start": start, "symbol": key},
        ).fetchone()
        if row is None:
            missing.append(pick)
            continue
        found[key] = Candidate(
            instrument_id=int(row[0]),
            symbol=str(row[1]),
            turnover=float(row[2] or 0),
            day_range=float(row[3] or 0),
            first_day=row[4] or HISTORY_BEFORE,
            days=int(row[5] or 0),
            named=True,
        )
    return list(found.values()), missing


def write_members(conn: Any, name: str, chosen: list[Candidate], as_of: date) -> int:
    """Replace the universe's membership. The name is validated: it goes into SQL by hand."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise ValueError(f"bad universe name {name!r}")
    conn.execute("DELETE FROM universe_members WHERE universe = %s", (name,))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO universe_members (universe, instrument_id, as_of) VALUES (%s, %s, %s)",
            [(name, c.instrument_id, as_of) for c in chosen],
        )
    return len(chosen)


def run_select(
    conn: Any,
    *,
    name: str,
    size: int = DEFAULT_SIZE,
    source: str = DEFAULT_SOURCE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_turnover: float = DEFAULT_MIN_TURNOVER,
    history_before: date = HISTORY_BEFORE,
    picks: tuple[str, ...] = NAMED_PICKS,
    dry_run: bool = False,
) -> UniversePlan:
    """Pick the universe and (unless ``dry_run``) store it."""
    candidates, missing = load_candidates(
        conn, source=source, lookback_days=lookback_days, picks=picks
    )
    chosen, liquid = choose(
        candidates,
        size=size,
        min_turnover=min_turnover,
        history_before=history_before,
        picks=picks,
    )
    plan = UniversePlan(
        name=name,
        source=source,
        size=size,
        min_turnover=min_turnover,
        lookback_days=lookback_days,
        considered=len(candidates),
        liquid=liquid,
        chosen=chosen,
        missing_picks=missing,
        dry_run=dry_run,
    )
    if not dry_run and chosen:
        as_of = conn.execute("SELECT max(trade_date) FROM candles_daily").fetchone()[0]
        plan.written = write_members(conn, name, chosen, as_of)
    return plan
