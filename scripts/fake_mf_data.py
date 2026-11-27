"""Write a FAKE AMFI scheme master and NAV history, then load them (compose-smoke, S6a).

No download and no real fund: the scheme codes are in a 9xxxxxx range AMFI does not issue and
every name starts with "ZZ Fake". The point is to exercise the real code path - the two
different AMFI column layouts, the universe filter, the COPY load and the resumable rule -
against a real Postgres, so CI catches a schema or SQL mistake before a migration is applied.

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
START = date(2019, 1, 1)
DAYS = 2600  # a bit over seven years: past the five-year rule S6b needs to rank at all

MASTER_HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
    "Net Asset Value;Date\n"
)
HISTORY_HEADER = (
    "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
    "Net Asset Value;Date\n"
)


def master() -> str:
    last = (START + timedelta(days=DAYS - 1)).strftime("%d-%b-%Y")
    parts = [MASTER_HEADER]
    for code, category, name, nav in (
        (EQUITY, "Equity Scheme - Large Cap Fund", "ZZ Fake Large Cap Fund", 180.0),
        (INDEX, "Index Funds - Equity Funds", "ZZ Fake Nifty 50 Index Fund", 90.0),
        (DEBT, "Debt Scheme - Liquid Fund", "ZZ Fake Liquid Fund", 25.0),
    ):
        parts.append(f"\nOpen Ended Schemes({category})\n\nZZ Fake Mutual Fund\n\n")
        parts.append(f"{code};INFZZZ{code};-;{name};Direct Plan;Growth;{nav:.4f};{last}\n")
    return "".join(parts)


def nav_on(day: int, base: float, wobble: float) -> float:
    """A rising series with one deliberate 25% fall and recovery, so the drawdown and the
    fall-and-recovery statistics have something real to find."""
    trend = base * (1.0 + 0.0003) ** day
    if 600 <= day < 700:  # the fall
        trend *= 1.0 - 0.25 * (day - 600) / 100
    elif 700 <= day < 900:  # the recovery
        trend *= 0.75 + 0.25 * (day - 700) / 200
    return round(trend + wobble * ((day % 7) - 3) * 0.01, 4)


def history() -> str:
    rows = [HISTORY_HEADER, "\nOpen Ended Schemes ( Equity Scheme - Large Cap Fund )\n\n"]
    rows.append("ZZ Fake Mutual Fund\n\n")
    for day in range(DAYS):
        when = (START + timedelta(days=day)).strftime("%d-%b-%Y")
        for code, name, base, wobble in (
            (EQUITY, "ZZ Fake Large Cap Fund", 100.0, 1.0),
            (INDEX, "ZZ Fake Nifty 50 Index Fund", 50.0, 0.5),
            (DEBT, "ZZ Fake Liquid Fund", 20.0, 0.0),
        ):
            nav = nav_on(day, base, wobble)
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
