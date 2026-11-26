"""Download AMFI NAV into Postgres, resumably and politely (S6a).

The official source (verified live 2026-11-26, both linked from AMFI's own
https://www.amfiindia.com/net-asset-value/nav-download page):

  master   https://portal.amfiindia.com/spages/NAVAll.txt      every scheme's latest NAV
  history  https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt=&todt=

AMFI serves at most 90 DAYS per history request, so a 15-year backfill is about 61 chunks.
Each chunk is roughly 70 MB and the server itself takes 60-110 seconds to produce it, which
makes this inherently polite: under one request a minute even with no added delay. AMFI needs
no login and no token.

Resumable in two places, because a 15-year backfill will be interrupted:
- the raw file: a chunk already on disk under ``DATA_DIR/raw/amfi/nav_history`` is not
  downloaded again (a partial download lands in ``.part`` and is redone);
- the database: a chunk already recorded ``loaded`` in ``mf_nav_chunks`` is not parsed again,
  so a complete rerun writes 0 rows.

Only the universe's NAV is stored. AMFI publishes about 14,500 schemes; keeping every one of
them for 15 years would be roughly 50 million rows, most of them debt and ETF schemes this
project does not look at. The scheme master keeps ALL schemes, so widening the universe later
needs no new download of the master.

mfapi.in and the mftool library are a documented FALLBACK for when AMFI is unavailable. This
module does not use them; if AMFI blocks or breaks, it stops and alerts rather than quietly
switching source, so the data's provenance is never in doubt.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_intel import mf
from market_intel.alerts import queue_alert
from market_intel.config import Settings
from market_intel.db import postgres
from market_intel.file_lock import safe_replace
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import get_logger

log = get_logger("market_intel.mf_fetch")

MASTER_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={a}&todt={b}"
USER_AGENT = "market-intel (personal research; polite, sequential)"

CHUNK_DAYS = 90  # AMFI's own maximum for one history request
DEFAULT_YEARS = 15
POLITE_GAP_SECONDS = 3.0
TIMEOUT_SECONDS = 420  # a 90-day chunk is ~70 MB and the server is slow to produce it
MIN_PLAUSIBLE_BYTES = 1000  # anything smaller is an error page, not NAV data
SOURCE = "amfi"


def raw_dir(settings: Settings, kind: str) -> Path:
    return settings.data_dir / "raw" / "amfi" / kind


def chunk_name(start: date, end: date) -> str:
    return f"{start:%Y%m%d}_{end:%Y%m%d}.txt"


def chunks(start: date, end: date, *, days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Whole-period chunks of at most ``days`` days, NEWEST FIRST.

    Newest first because an interrupted backfill should leave the most recent years in place:
    they are the ones every later question is asked about.
    """
    if end < start:
        raise ValueError("end is before start")
    out: list[tuple[date, date]] = []
    b = end
    while b >= start:
        a = max(start, b - timedelta(days=days - 1))
        out.append((a, b))
        b = a - timedelta(days=1)
    return out


class Blocked(RuntimeError):
    """AMFI refused or returned something that is not a NAV file: stop, do not retry hard."""


def _get(url: str, dest: Path, *, timeout: int = TIMEOUT_SECONDS) -> int:
    """Stream ``url`` to ``dest`` through a ``.part`` file. Returns the byte count."""
    part = dest.with_suffix(dest.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise Blocked(f"HTTP {response.status}")
            with part.open("wb") as handle:
                while True:
                    buf = response.read(1 << 20)
                    if not buf:
                        break
                    handle.write(buf)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise Blocked(f"HTTP {exc.code}") from exc
    size = part.stat().st_size
    if size < MIN_PLAUSIBLE_BYTES:
        part.unlink(missing_ok=True)
        raise Blocked(f"only {size} bytes: an error page, not NAV data")
    safe_replace(part, dest)  # Windows holds files open for a moment (S5i)
    return size


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- the scheme master --------------------------------------------------------------------
@dataclass
class MasterResult:
    schemes: int
    universe: int
    by_category: dict[str, int] = field(default_factory=dict)
    downloaded_bytes: int = 0
    path: Path | None = None


def download_master(settings: Settings, *, on: date | None = None, reuse: bool = True) -> Path:
    """Today's NAVAll.txt (every scheme's latest NAV), kept under DATA_DIR, never committed."""
    on = on or date.today()
    dest = raw_dir(settings, "master") / f"NAVAll_{on:%Y-%m-%d}.txt"
    if reuse and dest.exists() and dest.stat().st_size >= MIN_PLAUSIBLE_BYTES:
        log.info("amfi master already downloaded", extra={"bytes": dest.stat().st_size})
        return dest
    size = _get(MASTER_URL, dest)
    log.info("amfi master downloaded", extra={"bytes": size})
    return dest


def read_schemes(path: Path) -> list[mf.Scheme]:
    """Every scheme in a NAV file, one row each (the last one seen for a scheme code wins)."""
    by_code: dict[int, mf.Scheme] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for scheme, _ in mf.parse_nav_file(handle):
            by_code[scheme.scheme_code] = scheme
    return list(by_code.values())


SCHEME_UPSERT = """
INSERT INTO mf_schemes (scheme_code, scheme_name, amc, amfi_category, category, scheme_type,
                        plan, option, isin_growth, isin_reinvest, in_universe,
                        first_seen, last_seen)
VALUES (%(scheme_code)s, %(scheme_name)s, %(amc)s, %(amfi_category)s, %(category)s,
        %(scheme_type)s, %(plan)s, %(option)s, %(isin_growth)s, %(isin_reinvest)s,
        %(in_universe)s, %(seen)s, %(seen)s)
ON CONFLICT (scheme_code) DO UPDATE SET
    scheme_name = EXCLUDED.scheme_name,
    amc = EXCLUDED.amc,
    amfi_category = EXCLUDED.amfi_category,
    category = EXCLUDED.category,
    scheme_type = EXCLUDED.scheme_type,
    plan = EXCLUDED.plan,
    option = EXCLUDED.option,
    isin_growth = COALESCE(EXCLUDED.isin_growth, mf_schemes.isin_growth),
    isin_reinvest = COALESCE(EXCLUDED.isin_reinvest, mf_schemes.isin_reinvest),
    in_universe = EXCLUDED.in_universe,
    first_seen = LEAST(mf_schemes.first_seen, EXCLUDED.first_seen),
    last_seen = GREATEST(mf_schemes.last_seen, EXCLUDED.last_seen),
    updated_at = now()
"""


def store_schemes(conn: Any, schemes: Iterable[mf.Scheme], *, seen: date) -> MasterResult:
    rows = []
    by_category: dict[str, int] = {}
    universe = 0
    for s in schemes:
        wanted = mf.in_universe(s)
        universe += wanted
        if wanted:
            by_category[s.category] = by_category.get(s.category, 0) + 1
        rows.append(
            {
                "scheme_code": s.scheme_code,
                "scheme_name": s.scheme_name,
                "amc": s.amc,
                "amfi_category": s.amfi_category,
                "category": s.category,
                "scheme_type": s.scheme_type,
                "plan": s.plan,
                "option": s.option,
                "isin_growth": s.isin_growth,
                "isin_reinvest": s.isin_reinvest,
                "in_universe": wanted,
                "seen": seen,
            }
        )
    with conn.cursor() as cur:
        cur.executemany(SCHEME_UPSERT, rows)
    conn.commit()
    return MasterResult(schemes=len(rows), universe=universe, by_category=by_category)


def sync_master(settings: Settings, *, on: date | None = None, reuse: bool = True) -> MasterResult:
    """Download the scheme master and write every scheme, flagging the universe."""
    on = on or date.today()
    path = download_master(settings, on=on, reuse=reuse)
    schemes = read_schemes(path)
    with postgres.connect(settings.require("database")) as conn:
        result = store_schemes(conn, schemes, seen=on)
    result.path = path
    result.downloaded_bytes = path.stat().st_size
    log.info(
        "amfi scheme master stored",
        extra={"schemes": result.schemes, "universe": result.universe},
    )
    return result


# --- the NAV history ----------------------------------------------------------------------
@dataclass
class ChunkResult:
    start: date
    end: date
    rows: int
    bytes: int
    skipped: bool = False


@dataclass
class FetchResult:
    chunks: list[ChunkResult] = field(default_factory=list)
    stopped: str | None = None

    @property
    def rows(self) -> int:
        return sum(c.rows for c in self.chunks)

    @property
    def downloaded(self) -> int:
        return sum(c.bytes for c in self.chunks if not c.skipped)

    @property
    def loaded_chunks(self) -> int:
        return sum(1 for c in self.chunks if not c.skipped)


def loaded_chunks(conn: Any, *, source: str = SOURCE) -> set[tuple[date, date]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_date, to_date FROM mf_nav_chunks WHERE source = %s AND status = 'loaded'",
            (source,),
        )
        return {(a, b) for a, b in cur.fetchall()}


def universe_codes(conn: Any) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code FROM mf_schemes WHERE in_universe")
        return {row[0] for row in cur.fetchall()}


NAV_INSERT = """
INSERT INTO mf_nav (scheme_code, nav_date, nav, source)
SELECT scheme_code, nav_date, nav, %s FROM tmp_mf_nav
ON CONFLICT (scheme_code, nav_date) DO NOTHING
"""


def store_navs(conn: Any, rows: Iterable[mf.NavRow], *, source: str = SOURCE) -> int:
    """Insert NAV rows, ignoring ones already held. Returns the number actually written."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE tmp_mf_nav (scheme_code integer, nav_date date, "
            "nav numeric(14,4)) ON COMMIT DROP"
        )
        with cur.copy("COPY tmp_mf_nav (scheme_code, nav_date, nav) FROM STDIN") as copy:
            for row in rows:
                copy.write_row((row.scheme_code, row.nav_date, row.nav))
        cur.execute(NAV_INSERT, (source,))
        return cur.rowcount


def nav_rows_for(path: Path, codes: set[int]) -> Iterator[mf.NavRow]:
    """NAV rows of the universe's schemes only (see the module docstring on why)."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for _, row in mf.parse_nav_file(handle):
            if row.scheme_code in codes:
                yield row


def fetch(
    settings: Settings,
    *,
    years: int = DEFAULT_YEARS,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
    reuse: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
) -> FetchResult:
    """Back-fill ``years`` of daily NAV for the universe, one 90-day chunk at a time.

    Stops on a block (and alerts) rather than hammering AMFI or silently switching source.
    """
    end = end or date.today()
    start = start or (end - timedelta(days=int(round(years * 365.25))))
    plan = chunks(start, end)
    result = FetchResult()
    with postgres.connect(settings.require("database")) as conn:
        codes = universe_codes(conn)
        if not codes:
            raise RuntimeError("no universe schemes: run `mf fetch --master` first")
        done = loaded_chunks(conn)
        todo = [c for c in plan if c not in done]
        log.info(
            "amfi nav backfill planned",
            extra={"chunks": len(plan), "already_loaded": len(plan) - len(todo)},
        )
        if dry_run:
            result.stopped = f"dry run: {len(todo)} of {len(plan)} chunks to fetch"
            return result
        for n, (a, b) in enumerate(todo):
            if limit is not None and n >= limit:
                result.stopped = f"stopped after {limit} chunks (--limit)"
                break
            dest = raw_dir(settings, "nav_history") / chunk_name(a, b)
            try:
                if reuse and dest.exists() and dest.stat().st_size >= MIN_PLAUSIBLE_BYTES:
                    size = dest.stat().st_size
                    downloaded = False
                else:
                    size = _get(HISTORY_URL.format(a=_amfi_date(a), b=_amfi_date(b)), dest)
                    downloaded = True
            except (Blocked, OSError) as exc:
                result.stopped = f"{type(exc).__name__}: {exc}"
                queue_alert(
                    "error",
                    "AMFI NAV download stopped",
                    f"The {a} to {b} chunk failed: {exc}. "
                    "Nothing was switched to another source; rerun when AMFI is reachable.",
                    key="mf_fetch_blocked",
                )
                break
            written = store_navs(conn, nav_rows_for(dest, codes))
            record_chunk(conn, a, b, rows=written, size=size, sha256=sha256_of(dest))
            conn.commit()
            result.chunks.append(ChunkResult(a, b, written, size, skipped=not downloaded))
            log.info("amfi nav chunk loaded", extra={"from": str(a), "to": str(b), "rows": written})
            if downloaded:
                sleep(POLITE_GAP_SECONDS)
    return result


def _amfi_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def record_chunk(
    conn: Any, start: date, end: date, *, rows: int, size: int, sha256: str, source: str = SOURCE
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mf_nav_chunks (from_date, to_date, source, status, rows, bytes, sha256)
            VALUES (%s, %s, %s, 'loaded', %s, %s, %s)
            ON CONFLICT (from_date, to_date, source) DO UPDATE SET
                status = 'loaded', rows = EXCLUDED.rows, bytes = EXCLUDED.bytes,
                sha256 = EXCLUDED.sha256, fetched_at = now()
            """,
            (start, end, source, rows, size, sha256),
        )


def refresh_scheme_dates(conn: Any) -> int:
    """Set first_seen/last_seen from the NAV actually held (the master only knows today)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mf_schemes s
               SET first_seen = n.first, last_seen = n.last, updated_at = now()
              FROM (SELECT scheme_code, min(nav_date) AS first, max(nav_date) AS last
                      FROM mf_nav GROUP BY scheme_code) n
             WHERE n.scheme_code = s.scheme_code
               AND (s.first_seen, s.last_seen) IS DISTINCT FROM (n.first, n.last)
            """
        )
        return cur.rowcount


def run_fetch(settings: Settings, **kwargs: Any) -> FetchResult:
    """`mf fetch`: the scheme master, then the history, inside one recorded ingestion run."""
    master = kwargs.pop("master_only", False)
    with ingestion_run(settings, "mf_nav", source=SOURCE, params=_params(kwargs)) as run:
        result = sync_master(settings, reuse=kwargs.get("reuse", True))
        log.info("universe", extra={"schemes": result.universe})
        if master:
            return FetchResult(stopped=f"master only: {result.universe} schemes in the universe")
        fetched = fetch(settings, **kwargs)
        run.add_rows(fetched.rows)
        if fetched.stopped and not fetched.stopped.startswith(("dry run", "stopped after")):
            run.mark_partial(fetched.stopped)
        with postgres.connect(settings.require("database")) as conn:
            refresh_scheme_dates(conn)
            conn.commit()
        return fetched


def _params(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, date) else v) for k, v in kwargs.items() if k != "sleep"}


# --- reading it back for the stats engine -------------------------------------------------
def load_series(
    conn: Any, scheme_codes: Sequence[int] | None = None, *, category: str | None = None
) -> dict[int, list[tuple[date, float]]]:
    """NAV history per scheme, ascending, for the stats engine."""
    sql = [
        "SELECT n.scheme_code, n.nav_date, n.nav FROM mf_nav n",
        "JOIN mf_schemes s ON s.scheme_code = n.scheme_code",
        "WHERE s.in_universe",
    ]
    params: list[Any] = []
    if scheme_codes:
        sql.append("AND n.scheme_code = ANY(%s)")
        params.append(list(scheme_codes))
    if category:
        sql.append("AND s.category = %s")
        params.append(category)
    sql.append("ORDER BY n.scheme_code, n.nav_date")
    out: dict[int, list[tuple[date, float]]] = {}
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        for code, nav_date, nav in cur:
            out.setdefault(code, []).append((nav_date, float(nav)))
    return out


def scheme_rows(conn: Any, scheme_codes: Sequence[int] | None = None) -> dict[int, tuple[str, str]]:
    sql = "SELECT scheme_code, scheme_name, category FROM mf_schemes WHERE in_universe"
    params: list[Any] = []
    if scheme_codes:
        sql += " AND scheme_code = ANY(%s)"
        params.append(list(scheme_codes))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {code: (name, category) for code, name, category in cur.fetchall()}
