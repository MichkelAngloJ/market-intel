"""The owner's watchlist: up to 50 active NSE stocks (series EQ or BE), stored only in Postgres.

    uv run python -m market_intel.watchlist add SYMBOL [--note TEXT]
    uv run python -m market_intel.watchlist remove SYMBOL
    uv run python -m market_intel.watchlist list

Indices can't be added. Adding a BE stock prints a risk note. The contents are private: they
live only in the database, are never written to the repo or the docs, and the log records
counts only, never symbols.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import postgres
from market_intel.db.schema import WATCHLIST_NOTE_MAX
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id

COMPONENT = "watchlist"
MAX_ITEMS = 50
BE_NOTE = "series BE: trade-to-trade, often under exchange surveillance; higher risk"
STOCK_SERIES = ("EQ", "BE")

log = get_logger("market_intel.watchlist")

Connect = Callable[..., Any]


class WatchlistError(Exception):
    """A watchlist rule refused the change. Nothing was changed."""


@dataclass(frozen=True)
class Candidate:
    """An active NSE instrument with the requested symbol."""

    id: int
    segment: str
    series: str | None


def choose(symbol: str, candidates: list[Candidate], *, count: int, listed: set[int]) -> Candidate:
    """Apply the add rules to the instruments found for ``symbol`` (pure, for tests)."""
    stocks = [c for c in candidates if c.segment == "EQ" and c.series in STOCK_SERIES]
    if not stocks:
        if any(c.segment == "INDEX" for c in candidates):
            raise WatchlistError(
                f"{symbol} is an index; only stocks (series EQ or BE) can be added"
            )
        raise WatchlistError(
            f"{symbol} is not an active NSE stock of series EQ or BE "
            "(run the instruments refresh if it was listed recently)"
        )
    stock = stocks[0]
    if stock.id in listed:
        raise WatchlistError(f"{symbol} is already on the watchlist")
    if count >= MAX_ITEMS:
        raise WatchlistError(
            f"the watchlist is full ({MAX_ITEMS} stocks); remove one before adding another"
        )
    return stock


def normalise(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned or len(cleaned) > 60:
        raise WatchlistError("give a symbol, e.g. one shown by the instruments refresh")
    return cleaned


def add(conn: psycopg.Connection, symbol: str, note: str | None = None) -> list[str]:
    """Add a stock. Returns the lines to print (the BE note included)."""
    symbol = normalise(symbol)
    if note is not None and len(note) > WATCHLIST_NOTE_MAX:
        raise WatchlistError(f"the note is longer than {WATCHLIST_NOTE_MAX} characters")
    with conn.transaction():
        # Lock the table for this short transaction so two adds can't both pass the 50 check.
        conn.execute("LOCK TABLE watchlist IN SHARE ROW EXCLUSIVE MODE")
        rows = conn.execute(
            "SELECT id, segment, series FROM instruments"
            " WHERE exchange = 'NSE' AND symbol = %s AND is_active ORDER BY segment, id",
            (symbol,),
        ).fetchall()
        listed = {r[0] for r in conn.execute("SELECT instrument_id FROM watchlist").fetchall()}
        stock = choose(symbol, [Candidate(*r) for r in rows], count=len(listed), listed=listed)
        conn.execute(
            "INSERT INTO watchlist (instrument_id, note) VALUES (%s, %s)", (stock.id, note or None)
        )
    log.info("watchlist_added", items=len(listed) + 1)
    lines = [f"added {symbol} ({len(listed) + 1} of {MAX_ITEMS})"]
    if stock.series == "BE":
        lines.append(f"note: {BE_NOTE}")
    return lines


def remove(conn: psycopg.Connection, symbol: str) -> list[str]:
    symbol = normalise(symbol)
    # Matches by symbol even if the instrument has gone inactive since it was added.
    deleted = conn.execute(
        "DELETE FROM watchlist w USING instruments i"
        " WHERE w.instrument_id = i.id AND i.exchange = 'NSE' AND i.symbol = %s",
        (symbol,),
    ).rowcount
    if not deleted:
        raise WatchlistError(f"{symbol} is not on the watchlist")
    log.info("watchlist_removed", removed=deleted)
    return [f"removed {symbol}"]


LIST_SQL = (
    "SELECT i.symbol, i.name, i.series, i.sector, w.added_at, w.note, i.is_active"
    " FROM watchlist w JOIN instruments i ON i.id = w.instrument_id ORDER BY i.symbol"
)


def list_lines(conn: psycopg.Connection, tz: ZoneInfo) -> list[str]:
    rows = conn.execute(LIST_SQL).fetchall()
    if not rows:
        return [f"the watchlist is empty (0 of {MAX_ITEMS})"]
    lines = [f"watchlist: {len(rows)} of {MAX_ITEMS}"]
    lines.append(f"{'symbol':<14} {'series':<6} {'added':<10} {'sector':<28} name / note")
    for symbol, name, series, sector, added_at, note, active in rows:
        added = added_at.astimezone(tz).date().isoformat() if added_at else ""
        flag = " (INACTIVE)" if not active else ""
        extra = f" / {note}" if note else ""
        lines.append(
            f"{symbol:<14} {series or '':<6} {added:<10} {(sector or '-')[:28]:<28}"
            f" {name or ''}{flag}{extra}"
        )
    return lines


def run(settings: Settings, args: argparse.Namespace, connect: Connect | None = None) -> list[str]:
    with (connect or postgres.connect)(settings.require("database")) as conn:
        if args.command == "add":
            return add(conn, args.symbol, args.note)
        if args.command == "remove":
            return remove(conn, args.symbol)
        return list_lines(conn, ZoneInfo(settings.display_timezone))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m market_intel.watchlist")
    sub = parser.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add", help="add an active NSE stock (series EQ or BE)")
    add_p.add_argument("symbol")
    add_p.add_argument("--note", help=f"optional, at most {WATCHLIST_NOTE_MAX} characters")
    remove_p = sub.add_parser("remove", help="remove a stock")
    remove_p.add_argument("symbol")
    sub.add_parser("list", help="show the watchlist")
    return parser


def main(argv: list[str] | None = None, *, connect: Connect | None = None) -> int:
    """Entry point for ``python -m market_intel.watchlist``."""
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            print("\n".join(run(settings, args, connect)))
        except (WatchlistError, ConfigError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
