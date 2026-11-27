"""Write a FAKE AMFI scheme master and NAV history, then load them (compose-smoke, S6a/S6c).

No download and no real fund: the scheme codes are in a 9xxxxxx range AMFI does not issue and
every name starts with "ZZ Fake". The point is to exercise the real code path - the two
different AMFI column layouts, the universe filter, the COPY load and the resumable rule -
against a real Postgres, so CI catches a schema or SQL mistake before a migration is applied.

The funds deliberately do NOT all start on the same day, and one of them starts AFTER the
modelled crash. That is what lets CI exercise S6c: the shared window shrinks to what the late
fund covers, and that fund is the one the survived-a-fall flag has to mark as untested.

    uv run python scripts/fake_mf_data.py <directory>
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from market_intel import mf_fetch
from market_intel.config import get_settings
from market_intel.db import postgres

EQUITY = 9100001
INDEX = 9100002
DEBT = 9100003  # outside the universe: its NAV must never be stored
MIDCAP = 9100004  # a third equity-ish fund, so a reference index can be built at all
LATECOMER = 9100005  # starts after the crash: the "untested in a downturn" case

START = date(2017, 1, 1)
DAYS = 3300  # a bit over nine years, so a ten-year window is out of reach but five is not
LATE_START_DAY = 1300  # about 3.6 years in, well after the crash below ends
CRASH_FROM, CRASH_TO, CRASH_BACK = 600, 700, 900
CRASH_DEPTH = 0.35  # deep enough that the reference still clears 20% once the trend is netted

MASTER_HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
    "Net Asset Value;Date\n"
)
HISTORY_HEADER = (
    "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
    "Net Asset Value;Date\n"
)

# code, AMFI category heading, name, starting NAV, wobble, first day it reports
FUNDS = (
    (EQUITY, "Equity Scheme - Large Cap Fund", "ZZ Fake Large Cap Fund", 100.0, 1.0, 0),
    (INDEX, "Index Funds - Equity Funds", "ZZ Fake Nifty 50 Index Fund", 50.0, 0.5, 0),
    (DEBT, "Debt Scheme - Liquid Fund", "ZZ Fake Liquid Fund", 20.0, 0.0, 0),
    (MIDCAP, "Equity Scheme - Mid Cap Fund", "ZZ Fake Mid Cap Fund", 75.0, 1.5, 0),
    (
        LATECOMER,
        "Equity Scheme - Flexi Cap Fund",
        "ZZ Fake New Flexi Cap Fund",
        10.0,
        1.0,
        LATE_START_DAY,
    ),
)


def master() -> str:
    last = (START + timedelta(days=DAYS - 1)).strftime("%d-%b-%Y")
    parts = [MASTER_HEADER]
    for code, category, name, nav, _wobble, _from_day in FUNDS:
        parts.append(f"\nOpen Ended Schemes({category})\n\nZZ Fake Mutual Fund\n\n")
        parts.append(f"{code};INFZZZ{code};-;{name};Direct Plan;Growth;{nav:.4f};{last}\n")
    return "".join(parts)


def nav_on(day: int, base: float, wobble: float, *, crashes: bool = True) -> float:
    """A rising series with one deliberate crash and recovery, so the drawdown, the
    fall-and-recovery statistics and the market reference all have something real to find."""
    trend = base * (1.0 + 0.0003) ** day
    if crashes:
        if CRASH_FROM <= day < CRASH_TO:
            trend *= 1.0 - CRASH_DEPTH * (day - CRASH_FROM) / (CRASH_TO - CRASH_FROM)
        elif CRASH_TO <= day < CRASH_BACK:
            trend *= (1.0 - CRASH_DEPTH) + CRASH_DEPTH * (day - CRASH_TO) / (CRASH_BACK - CRASH_TO)
    return round(trend + wobble * ((day % 7) - 3) * 0.01, 4)


def history() -> str:
    rows = [HISTORY_HEADER, "\nOpen Ended Schemes ( Equity Scheme - Large Cap Fund )\n\n"]
    rows.append("ZZ Fake Mutual Fund\n\n")
    for day in range(DAYS):
        when = (START + timedelta(days=day)).strftime("%d-%b-%Y")
        for code, _category, name, base, wobble, from_day in FUNDS:
            if day < from_day:
                continue  # this fund did not exist yet
            # The latecomer's own NAV starts at its base on its first day, so its history is
            # a clean stretch with no crash in it at all.
            nav = nav_on(day - from_day, base, wobble, crashes=from_day == 0)
            rows.append(
                f"{code};{name} - Direct - Growth;Direct Plan;Growth;INFZZZ{code};-;"
                f"{nav:.4f};{when}\n"
            )
    return "".join(rows)


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    master_path, history_path = out / "zz_master.txt", out / "zz_history.txt"
    master_path.write_text(master(), encoding="utf-8")
    history_path.write_text(history(), encoding="utf-8")

    settings = get_settings()
    with postgres.connect(settings.require("database")) as conn:
        result = mf_fetch.store_schemes(conn, mf_fetch.read_schemes(master_path), seen=START)
        print(f"schemes stored {result.schemes}, in the universe {result.universe}")
        codes = mf_fetch.universe_codes(conn)
        if DEBT in codes:
            raise SystemExit("the debt fund must not be in the universe")

        first = mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history_path, codes))
        mf_fetch.record_chunk(
            conn, START, START + timedelta(days=DAYS - 1), rows=first, size=0, sha256="0" * 64
        )
        second = mf_fetch.store_navs(conn, mf_fetch.nav_rows_for(history_path, codes))
        mf_fetch.refresh_scheme_dates(conn)
        conn.commit()

    print(f"first load wrote {first} rows")
    print(f"second load wrote {second} rows")
    if second != 0:
        raise SystemExit(f"a rerun must write nothing, it wrote {second}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
