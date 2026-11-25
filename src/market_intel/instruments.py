"""NSE instruments in Postgres: the daily refresh from Upstox's file and the sector import.

    uv run python -m market_intel.instruments refresh [--file PATH] [--dry-run]
    uv run python -m market_intel.instruments import-sectors PATH [--dry-run]

refresh (inside ``ingestion_run(job="instruments_refresh", source="upstox")``, one transaction
through the app login, logs to ``logs/instruments.jsonl``):

- Keyed by ``instrument_key``. Kept: NSE stocks of series EQ and BE, and all NSE indices
  (:mod:`market_intel.sources.upstox_instruments`).
- New instruments are inserted (``first_seen_on`` = today, IST); known ones are updated only
  when something changed, so a second run on the same file makes 0 changes. ``last_seen_on``
  moves to today for every listed instrument (not counted as a change).
- A changed symbol writes an ``instrument_symbol_history`` row.
- Instruments missing from the file become inactive; rows are never deleted and
  ``delisted_on`` stays NULL (absence isn't proof of delisting). A listed inactive instrument
  becomes active again.
- Safety valve: if the file keeps fewer than ``INSTRUMENTS_MIN_KEEP_RATIO`` x the currently
  active instruments (and some are active), nothing changes, the run is marked failed and a
  WARNING alert is queued.

import-sectors reads the "Nifty 500" list CSV that the owner downloads by hand from the NSE
Indices website and sets ``sector`` from its Industry column, matched by ISIN (fallback:
symbol with series EQ). Empty values never overwrite.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg

from market_intel.alerts import queue_alert
from market_intel.config import ConfigError, Settings, get_settings
from market_intel.db import postgres
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    mask_text,
    new_run_id,
)
from market_intel.sources.records import Instrument
from market_intel.sources.upstox_instruments import (
    SOURCE,
    Download,
    InstrumentFileError,
    ParseResult,
    download,
    parse_bse_codes,
    parse_file,
    save_validators,
)

COMPONENT = "instruments"
JOB = "instruments_refresh"
IST = ZoneInfo("Asia/Kolkata")
REFUSED_TITLE = "Instrument refresh refused: file too small"
# The file's equity and index keys start with these; other instrument rows are left alone.
MANAGED_PREFIXES = ("NSE_EQ", "NSE_INDEX")
COMPARED = ("segment", "symbol", "name", "series", "isin", "tick_size", "lot_size")

log = get_logger("market_intel.instruments")


def ist_today() -> date:
    return datetime.now(IST).date()


# --- refresh: plan (pure) ----------------------------------------------------------------


@dataclass(frozen=True)
class Existing:
    """One instrument row the refresh manages (its key starts with NSE_EQ| or NSE_INDEX|)."""

    id: int
    instrument_key: str
    segment: str
    symbol: str
    name: str | None
    series: str | None
    isin: str | None
    tick_size: Decimal | None
    lot_size: int | None
    is_active: bool
    last_seen_on: date | None


def _values(inst: Instrument) -> dict[str, Any]:
    return {f: getattr(inst, f) for f in COMPARED}


@dataclass
class Plan:
    """What a refresh will change. Built by :func:`plan_refresh`, applied by :func:`apply_plan`."""

    today: date
    deactivate: list[int] = field(default_factory=list)  # active, missing from the file
    move_off: list[int] = field(default_factory=list)  # active rows changing symbol
    updates: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    history: list[tuple[int, str, str]] = field(default_factory=list)  # id, old, new
    inserts: list[Instrument] = field(default_factory=list)
    seen: list[int] = field(default_factory=list)  # unchanged rows whose last_seen_on moves
    updated: int = 0
    reactivated: int = 0

    @property
    def changes(self) -> int:
        return len(self.inserts) + self.updated + self.reactivated + len(self.deactivate)


class RefreshRefused(Exception):
    """The safety valve: the file is much smaller than the active list. Nothing changed."""

    def __init__(self, kept: int, active: int, ratio: float) -> None:
        self.kept, self.active, self.ratio = kept, active, ratio
        super().__init__(
            f"the file keeps {kept} instruments, fewer than {ratio:g} x the {active} active ones; "
            "nothing was changed"
        )


def plan_refresh(
    existing: Iterable[Existing],
    instruments: Sequence[Instrument],
    *,
    today: date,
    min_keep_ratio: float,
) -> Plan:
    """Compare the file with the table. Raises :class:`RefreshRefused` (the safety valve)."""
    by_key = {row.instrument_key: row for row in existing}
    active = sum(row.is_active for row in by_key.values())
    if active > 0 and len(instruments) < min_keep_ratio * active:
        raise RefreshRefused(len(instruments), active, min_keep_ratio)

    plan = Plan(today=today)
    file_keys = {inst.instrument_key for inst in instruments}
    plan.deactivate = sorted(
        row.id for row in by_key.values() if row.is_active and row.instrument_key not in file_keys
    )
    for inst in instruments:
        row = by_key.get(inst.instrument_key or "")
        if row is None:
            plan.inserts.append(inst)
            continue
        new = _values(inst)
        changed = any(getattr(row, f) != new[f] for f in COMPARED)
        if row.symbol != inst.symbol:
            plan.history.append((row.id, row.symbol, inst.symbol))
            if row.is_active:
                plan.move_off.append(row.id)
        if not row.is_active:
            plan.reactivated += 1
            plan.updates.append((row.id, new))
        elif changed:
            plan.updated += 1
            plan.updates.append((row.id, new))
        elif row.last_seen_on != today:
            plan.seen.append(row.id)
    return plan


# --- refresh: database -------------------------------------------------------------------

LOAD_SQL = (
    "SELECT id, instrument_key, segment, symbol, name, series, isin, tick_size, lot_size,"
    " is_active, last_seen_on FROM instruments"
    " WHERE exchange = 'NSE' AND split_part(instrument_key, '|', 1) = ANY(%s)"
    " ORDER BY id FOR UPDATE"
)


def load_existing(conn: psycopg.Connection) -> list[Existing]:
    rows = conn.execute(LOAD_SQL, (list(MANAGED_PREFIXES),)).fetchall()
    return [Existing(*row) for row in rows]


def apply_plan(conn: psycopg.Connection, plan: Plan) -> None:
    """Write a plan. Run inside one transaction.

    Order matters for the unique index on active symbols: first deactivate the missing rows and
    the rows whose symbol changes, then write the updates, then insert the new rows.
    """
    off = plan.deactivate + plan.move_off
    if off:
        conn.execute("UPDATE instruments SET is_active = false WHERE id = ANY(%s)", (off,))
    with conn.cursor() as cur:
        if plan.updates:
            cur.executemany(
                "UPDATE instruments SET segment = %s, symbol = %s, name = %s, series = %s,"
                " isin = %s, tick_size = %s, lot_size = %s, is_active = true,"
                " last_seen_on = %s WHERE id = %s",
                [(*(new[f] for f in COMPARED), plan.today, row_id) for row_id, new in plan.updates],
            )
        if plan.history:
            cur.executemany(
                "INSERT INTO instrument_symbol_history"
                " (instrument_id, old_symbol, new_symbol, changed_on) VALUES (%s, %s, %s, %s)",
                [(*h, plan.today) for h in plan.history],
            )
        if plan.inserts:
            cur.executemany(
                "INSERT INTO instruments (exchange, segment, symbol, name, series, isin,"
                " instrument_key, tick_size, lot_size, is_active, first_seen_on, last_seen_on)"
                " VALUES ('NSE', %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)",
                [
                    (
                        i.segment,
                        i.symbol,
                        i.name,
                        i.series,
                        i.isin,
                        i.instrument_key,
                        i.tick_size,
                        i.lot_size,
                        plan.today,
                        plan.today,
                    )
                    for i in plan.inserts
                ],
            )
    if plan.seen:
        conn.execute(
            "UPDATE instruments SET last_seen_on = %s WHERE id = ANY(%s)", (plan.today, plan.seen)
        )


def refresh_in_transaction(
    conn: psycopg.Connection,
    instruments: Sequence[Instrument],
    *,
    today: date,
    min_keep_ratio: float,
) -> Plan:
    """Load, plan and apply inside one transaction (a savepoint if one is already open)."""
    with conn.transaction():
        plan = plan_refresh(
            load_existing(conn), instruments, today=today, min_keep_ratio=min_keep_ratio
        )
        apply_plan(conn, plan)
    return plan


# --- refresh: command --------------------------------------------------------------------


@dataclass
class RefreshSummary:
    dry_run: bool = False
    not_modified: bool = False
    plan: Plan | None = None
    parsed: ParseResult | None = None
    bse: BseResult | None = None

    def all_lines(self) -> list[str]:
        """The NSE summary plus the BSE-code line (when the BSE step ran or was skipped)."""
        return self.lines() + (self.bse.lines() if self.bse else [])

    def lines(self) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if self.dry_run else ""
        if self.not_modified:
            return [f"{prefix}instrument file not modified since the last refresh: nothing to do"]
        assert self.plan is not None and self.parsed is not None
        p, parsed = self.plan, self.parsed
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(parsed.skipped_by_segment.items()))
        counts = parsed.count_by()
        return [
            f"{prefix}instruments refresh: {p.changes} changes",
            f"  inserted {len(p.inserts)}, updated {p.updated}, symbol changes {len(p.history)},"
            f" deactivated {len(p.deactivate)}, reactivated {p.reactivated}",
            f"  kept from file: EQ {counts['EQ']}, BE {counts['BE']}, indices {counts['INDEX']}",
            f"  skipped by segment: {skipped or 'none'}; malformed: {parsed.malformed}",
        ]


Connect = Callable[..., Any]


def run_refresh(
    settings: Settings,
    *,
    file: Path | None = None,
    dry_run: bool = False,
    today: date | None = None,
    client: httpx.Client | None = None,
    connect: Connect | None = None,
    bse: bool = False,
    bse_file: Path | None = None,
) -> RefreshSummary:
    """The ``refresh`` command. Raises RefreshRefused / InstrumentFileError on refusal.

    ``bse=True`` also maps BSE scrip codes from Upstox's BSE file (or ``bse_file``) after the
    NSE part; if only that step fails, the run is partial. The command turns it on."""
    today = today or ist_today()
    conn_factory = connect or postgres.connect
    url = settings.upstox_instruments_url
    summary = RefreshSummary(dry_run=dry_run)

    if dry_run:  # full download to a temporary file, changes rolled back, no run recorded
        got = (
            Download(path=file)
            if file
            else download(
                url, settings.data_dir, today, conditional=False, save=False, client=client
            )
        )
        assert got.path is not None
        try:
            summary.parsed, summary.plan = _dry_run(conn_factory, settings, got.path, today)
        finally:
            if not file:
                got.path.unlink(missing_ok=True)
        if bse:
            summary.bse, _ = refresh_bse(
                settings, conn_factory, today, client=client, bse_file=bse_file, dry_run=True
            )
        return summary

    params: dict[str, Any] = {"file": file.name if file else None, "download": file is None}
    if bse:
        params["bse"] = True
    bse_got: Download | None = None
    try:
        with ingestion_run(settings, JOB, source=SOURCE, params=params, connect=connect) as run:
            got = (
                Download(path=file)
                if file
                else download(url, settings.data_dir, today, client=client)
            )
            if got.not_modified:
                summary.not_modified = True
                log.info("instruments_not_modified")
            else:
                assert got.path is not None
                summary.parsed = parse_file(got.path)
                db = settings.require("database")
                with conn_factory(db) as conn:
                    summary.plan = refresh_in_transaction(
                        conn,
                        summary.parsed.instruments,
                        today=today,
                        min_keep_ratio=settings.instruments_min_keep_ratio,
                    )
                run.add_rows(summary.plan.changes)
            if bse:
                summary.bse, bse_got = refresh_bse(
                    settings, conn_factory, today, client=client, bse_file=bse_file
                )
                if summary.bse.plan:
                    run.add_rows(summary.bse.plan.changes)
                if summary.bse.error:
                    run.mark_partial(f"BSE codes failed: {summary.bse.error}")
    except RefreshRefused as exc:
        log.warning("instruments_refresh_refused", kept=exc.kept, active=exc.active)
        queue_alert(
            "warning",
            REFUSED_TITLE,
            f"The instrument refresh was refused: {exc}. Check the Upstox instrument file and "
            "run the refresh again; see the runbook (Instruments).",
            context={"kept": exc.kept, "active": exc.active, "min_keep_ratio": exc.ratio},
        )
        raise
    if not file and not got.not_modified:
        save_validators(settings.data_dir, url, got)  # only after the refresh committed
    if bse and not bse_file and bse_got is not None and not bse_got.not_modified:
        save_validators(settings.data_dir, settings.upstox_bse_instruments_url, bse_got, "BSE")
    if summary.plan is not None and summary.parsed is not None:
        log.info(
            "instruments_refreshed",
            changes=summary.plan.changes,
            inserted=len(summary.plan.inserts),
            deactivated=len(summary.plan.deactivate),
            malformed=summary.parsed.malformed,
        )
    return summary


def _dry_run(
    conn_factory: Connect, settings: Settings, path: Path, today: date
) -> tuple[ParseResult, Plan]:
    """Plan and apply in a transaction that is always rolled back."""
    parsed = parse_file(path)
    plan: Plan | None = None
    with conn_factory(settings.require("database")) as conn:
        try:
            with conn.transaction():
                plan = refresh_in_transaction(
                    conn,
                    parsed.instruments,
                    today=today,
                    min_keep_ratio=settings.instruments_min_keep_ratio,
                )
                raise psycopg.Rollback()
        except psycopg.Rollback:  # fakes in tests may not swallow it
            pass
    assert plan is not None
    return parsed, plan


# --- BSE scrip codes (from Upstox's public BSE file, by ISIN) ----------------------------

BSE_ROWS_SQL = (
    "SELECT id, isin, bse_code FROM instruments"
    " WHERE exchange = 'NSE' AND segment = 'EQ' AND isin IS NOT NULL ORDER BY id FOR UPDATE"
)
BSE_COUNT_SQL = "SELECT count(*) FROM instruments WHERE bse_code IS NOT NULL"


class BseRefused(Exception):
    """The BSE file maps far fewer stocks than already have a code. Nothing was changed."""


@dataclass
class BsePlan:
    set_codes: list[tuple[str, int]] = field(default_factory=list)  # code, instrument id
    clear: list[int] = field(default_factory=list)
    mapped: int = 0  # NSE stocks with a code after the plan

    @property
    def changes(self) -> int:
        return len(self.set_codes) + len([i for i in self.clear if i not in self._set_ids])

    @property
    def _set_ids(self) -> set[int]:
        return {row_id for _, row_id in self.set_codes}


def plan_bse_codes(
    rows: Iterable[tuple[int, str | None, str | None]],
    by_isin: dict[str, str],
    *,
    min_keep_ratio: float,
) -> BsePlan:
    """Give each NSE stock (id, isin, current code) the code of its ISIN, or none. Refuses
    (BseRefused) if fewer than ``min_keep_ratio`` x the currently coded stocks keep a code."""
    plan = BsePlan()
    current = 0
    for row_id, isin, code in rows:
        current += code is not None
        wanted = by_isin.get(isin or "")
        plan.mapped += wanted is not None
        if wanted == code:
            continue
        if code is not None:
            plan.clear.append(row_id)  # first, so a code can move between stocks
        if wanted is not None:
            plan.set_codes.append((wanted, row_id))
    if current > 0 and plan.mapped < min_keep_ratio * current:
        raise BseRefused(
            f"the BSE file maps {plan.mapped} stocks, fewer than {min_keep_ratio:g} x the "
            f"{current} that have a code; nothing was changed"
        )
    return plan


def apply_bse_plan(conn: psycopg.Connection, plan: BsePlan) -> None:
    if plan.clear:
        conn.execute("UPDATE instruments SET bse_code = NULL WHERE id = ANY(%s)", (plan.clear,))
    if plan.set_codes:
        with conn.cursor() as cur:
            cur.executemany("UPDATE instruments SET bse_code = %s WHERE id = %s", plan.set_codes)


@dataclass
class BseResult:
    not_modified: bool = False
    codes_in_file: int = 0
    malformed: int = 0
    plan: BsePlan | None = None
    mapped_total: int | None = None  # instruments with a bse_code after the step
    error: str | None = None
    skipped: str | None = None

    def lines(self) -> list[str]:
        if self.skipped:
            return [f"  BSE codes: skipped ({self.skipped})"]
        if self.error:
            return [f"  BSE codes: FAILED ({self.error}); the NSE refresh above still counts"]
        total = "-" if self.mapped_total is None else self.mapped_total
        if self.not_modified:
            return [f"  BSE codes: file not modified; {total} instruments have a bse_code"]
        assert self.plan is not None
        return [
            f"  BSE codes: {total} instruments have a bse_code (set {len(self.plan.set_codes)},"
            f" cleared {len(self.plan.clear)}); codes in file {self.codes_in_file},"
            f" malformed {self.malformed}"
        ]


def refresh_bse(
    settings: Settings,
    conn_factory: Connect,
    today: date,
    *,
    client: httpx.Client | None = None,
    bse_file: Path | None = None,
    dry_run: bool = False,
) -> tuple[BseResult, Download | None]:
    """Download (or read) the BSE file and map its codes onto NSE stocks by ISIN, in its own
    transaction. File problems and the BSE safety valve end in ``error`` (the caller marks
    the run partial); database errors propagate."""
    url = settings.upstox_bse_instruments_url
    result = BseResult()
    got: Download | None = None
    try:
        if bse_file:
            got = Download(path=bse_file)
        else:
            got = download(
                url,
                settings.data_dir,
                today,
                conditional=not dry_run,
                save=not dry_run,
                client=client,
                prefix="BSE",
            )
        db = settings.require("database")
        if got.not_modified:
            result.not_modified = True
            with conn_factory(db) as conn:
                result.mapped_total = conn.execute(BSE_COUNT_SQL).fetchone()[0]
            return result, got
        assert got.path is not None
        codes = parse_bse_codes(got.path)
        result.codes_in_file, result.malformed = len(codes.by_isin), codes.malformed
        with conn_factory(db) as conn:
            try:
                with conn.transaction():
                    rows = conn.execute(BSE_ROWS_SQL).fetchall()
                    result.plan = plan_bse_codes(
                        rows, codes.by_isin, min_keep_ratio=settings.instruments_min_keep_ratio
                    )
                    apply_bse_plan(conn, result.plan)
                    result.mapped_total = conn.execute(BSE_COUNT_SQL).fetchone()[0]
                    if dry_run:
                        raise psycopg.Rollback()
            except psycopg.Rollback:
                pass
    except (InstrumentFileError, BseRefused, httpx.HTTPError) as exc:
        result.error = mask_text(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("bse_codes_failed", error=result.error)
        return result, None
    finally:
        if dry_run and got is not None and got.path is not None and not bse_file:
            got.path.unlink(missing_ok=True)
    log.info("bse_codes_refreshed", mapped=result.mapped_total, dry_run=dry_run)
    return result, got


# --- sector import -----------------------------------------------------------------------

SECTOR_FILE_MAX_BYTES = 5 * 1024 * 1024
# The Nifty 500 list CSV's columns (NSE Indices website, file ind_nifty500list.csv).
SECTOR_HEADERS = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")


class SectorFileError(Exception):
    """The CSV is too big, unreadable or has unexpected headers. Nothing was changed."""


@dataclass(frozen=True)
class SectorRow:
    symbol: str
    series: str
    isin: str
    industry: str


def read_sector_csv(path: Path) -> list[SectorRow]:
    """Read and check the Nifty 500 list CSV (an untrusted file: size cap, csv module only)."""
    if path.stat().st_size > SECTOR_FILE_MAX_BYTES:
        raise SectorFileError(f"{path.name} is larger than {SECTOR_FILE_MAX_BYTES} bytes")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise SectorFileError(f"{path.name} is not UTF-8 text") from None
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader, [])]
    if tuple(header) != SECTOR_HEADERS:
        raise SectorFileError(
            f"unexpected headers in {path.name}: {header!r}; expected {list(SECTOR_HEADERS)!r}. "
            "Check that this is the Nifty 500 list CSV from the NSE Indices website."
        )
    rows = []
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        if len(line) != len(SECTOR_HEADERS):
            raise SectorFileError(f"row {reader.line_num} has {len(line)} columns, not 5")
        _company, industry, symbol, series, isin = (cell.strip() for cell in line)
        rows.append(SectorRow(symbol=symbol, series=series, isin=isin, industry=industry))
    return rows


@dataclass
class SectorPlan:
    updates: list[tuple[str, int]] = field(default_factory=list)  # sector, instrument id
    matched_ids: list[int] = field(default_factory=list)  # the universe's members
    universe: str = "nifty500"
    matched: int = 0
    unchanged: int = 0
    unmatched: int = 0
    empty: int = 0

    def lines(self, dry_run: bool) -> list[str]:
        prefix = "DRY RUN (nothing saved): " if dry_run else ""
        return [
            f"{prefix}sector import: matched {self.matched}, unmatched {self.unmatched}",
            f"  sectors set or changed {len(self.updates)}, unchanged {self.unchanged},"
            f" empty industry skipped {self.empty}",
            f"  universe {self.universe}: {len(self.matched_ids)} members",
        ]


def plan_sectors(
    rows: Iterable[SectorRow],
    instruments: Iterable[tuple[int, str | None, str, str | None, str | None]],
) -> SectorPlan:
    """Match CSV rows to NSE stocks (id, isin, symbol, series, sector) by ISIN, else by symbol
    with series EQ. Never sets an empty sector."""
    by_isin: dict[str, tuple[int, str | None]] = {}
    by_symbol: dict[str, tuple[int, str | None]] = {}
    for row_id, isin, symbol, series, sector in instruments:
        if isin:
            by_isin[isin] = (row_id, sector)
        if series == "EQ":
            by_symbol[symbol] = (row_id, sector)
    plan = SectorPlan()
    seen: set[int] = set()
    for row in rows:
        hit = by_isin.get(row.isin) or (by_symbol.get(row.symbol) if row.series == "EQ" else None)
        if hit is None:
            plan.unmatched += 1
            continue
        plan.matched += 1
        row_id, current = hit
        if row_id not in plan.matched_ids:
            plan.matched_ids.append(row_id)
        if not row.industry:
            plan.empty += 1
        elif current == row.industry or row_id in seen:
            plan.unchanged += 1
        else:
            plan.updates.append((row.industry, row_id))
            seen.add(row_id)
    return plan


SECTOR_LOAD_SQL = (
    "SELECT id, isin, symbol, series, sector FROM instruments"
    " WHERE exchange = 'NSE' AND segment = 'EQ' AND is_active ORDER BY id"
)


def run_import_sectors(
    settings: Settings,
    path: Path,
    *,
    dry_run: bool = False,
    connect: Connect | None = None,
    universe: str = "nifty500",
) -> SectorPlan:
    """Sectors from an NSE index list CSV (Nifty 500, Nifty 50, ...); the matched stocks become
    ``universe``'s members as of today (IST)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", universe):
        raise SectorFileError("universe names are lower-case letters, digits and _")
    rows = read_sector_csv(path)
    with (connect or postgres.connect)(settings.require("database")) as conn:
        try:
            with conn.transaction():
                plan = plan_sectors(rows, conn.execute(SECTOR_LOAD_SQL).fetchall())
                plan.universe = universe
                if plan.updates:
                    with conn.cursor() as cur:
                        cur.executemany(
                            "UPDATE instruments SET sector = %s WHERE id = %s", plan.updates
                        )
                # The matched stocks are the universe as of today (IST); one statement each,
                # so the list is replaced as a whole. (The name was checked above.)
                conn.execute(f"DELETE FROM universe_members WHERE universe = '{universe}'")
                conn.execute(
                    "INSERT INTO universe_members (universe, instrument_id, as_of)"
                    f" SELECT '{universe}', unnest(%s::bigint[]), %s",
                    (plan.matched_ids, ist_today()),
                )
                if dry_run:
                    raise psycopg.Rollback()
        except psycopg.Rollback:
            pass
    log.info(
        "sectors_imported",
        dry_run=dry_run,
        matched=plan.matched,
        unmatched=plan.unmatched,
        changed=len(plan.updates),
    )
    return plan


# --- entry point -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m market_intel.instruments")
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="refresh NSE instruments from Upstox's file")
    refresh.add_argument("--file", type=Path, help="use this local .json.gz instead of downloading")
    refresh.add_argument("--dry-run", action="store_true", help="show the changes, save nothing")
    refresh.add_argument(
        "--bse-file",
        type=Path,
        help="use this local BSE .json.gz for the BSE codes (with --file, BSE codes are skipped"
        " unless this is given)",
    )
    sectors = sub.add_parser("import-sectors", help="set sectors from the Nifty 500 list CSV")
    sectors.add_argument("path", type=Path, help="the CSV downloaded from the NSE Indices website")
    sectors.add_argument("--dry-run", action="store_true", help="show the changes, save nothing")
    sectors.add_argument(
        "--universe", default="nifty500", help="the universe the matched stocks form"
    )
    return parser


def main(argv: list[str] | None = None, *, connect: Connect | None = None) -> int:
    """Entry point for ``python -m market_intel.instruments``."""
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            if args.command == "refresh":
                if args.file and not args.file.is_file():
                    print(f"No such file: {args.file}", file=sys.stderr)
                    return 2
                use_bse = args.file is None or args.bse_file is not None
                summary = run_refresh(
                    settings,
                    file=args.file,
                    dry_run=args.dry_run,
                    connect=connect,
                    bse=use_bse,
                    bse_file=args.bse_file,
                )
                if not use_bse:
                    summary.bse = BseResult(skipped="--file without --bse-file")
                print("\n".join(summary.all_lines()))
            else:
                if not args.path.is_file():
                    print(f"No such file: {args.path}", file=sys.stderr)
                    return 2
                plan = run_import_sectors(
                    settings,
                    args.path,
                    dry_run=args.dry_run,
                    connect=connect,
                    universe=args.universe,
                )
                print("\n".join(plan.lines(args.dry_run)))
        except RefreshRefused as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 1
        except (InstrumentFileError, SectorFileError, ConfigError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except httpx.HTTPError as exc:
            print(f"Download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
