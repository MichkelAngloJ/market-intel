"""Instrument refresh (plan, idempotence, deactivate/reactivate, symbol history, safety valve)
and the sector import. No network, no database: an in-memory table stands in for Postgres
(the real SQL runs in tests/integration/test_instruments_db.py)."""

import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from instrument_fakes import Server, eq, gz_bytes, index, write_gz

from market_intel import alerts, instruments
from market_intel.config import load_settings
from market_intel.instruments import (
    Existing,
    Plan,
    RefreshRefused,
    SectorFileError,
    SectorRow,
    plan_refresh,
    plan_sectors,
    read_sector_csv,
)
from market_intel.logging_setup import configure_logging, log_file_name
from market_intel.sources.records import Instrument

DAY1, DAY2 = date(2026, 11, 22), date(2026, 11, 23)
FAKE_PW = "fake-pass-654"
FAKE_APP_PW = " ".join(["fakq", "wxyz", "abcd", "efgh"])  # built from parts: not a real value


def stock(symbol: str, isin: str, series: str = "EQ", **kw) -> Instrument:
    return Instrument(
        source="upstox",
        segment="EQ",
        symbol=symbol,
        name=f"{symbol} FAKE LIMITED",
        series=series,
        isin=isin,
        instrument_key=f"NSE_EQ|{isin}",
        tick_size=Decimal("0.05"),
        lot_size=1,
        **kw,
    )


class Table:
    """The managed instrument rows in memory, applying plans like apply_plan does."""

    def __init__(self):
        self.rows: dict[int, Existing] = {}
        self.history: list[tuple[int, str, str, date]] = []
        self.next_id = 1

    def existing(self) -> list[Existing]:
        return list(self.rows.values())

    def apply(self, plan: Plan) -> None:
        for row_id in plan.deactivate + plan.move_off:
            self.rows[row_id] = replace(self.rows[row_id], is_active=False)
        for row_id, new in plan.updates:
            self.rows[row_id] = replace(
                self.rows[row_id], **new, is_active=True, last_seen_on=plan.today
            )
        self.history += [(*h, plan.today) for h in plan.history]
        for inst in plan.inserts:
            self.rows[self.next_id] = Existing(
                id=self.next_id,
                instrument_key=inst.instrument_key,
                segment=inst.segment,
                symbol=inst.symbol,
                name=inst.name,
                series=inst.series,
                isin=inst.isin,
                tick_size=inst.tick_size,
                lot_size=inst.lot_size,
                is_active=True,
                last_seen_on=plan.today,
            )
            self.next_id += 1
        for row_id in plan.seen:
            self.rows[row_id] = replace(self.rows[row_id], last_seen_on=plan.today)
        active = [(r.segment, r.symbol) for r in self.rows.values() if r.is_active]
        assert len(active) == len(set(active)), "unique index on active symbols violated"

    def refresh(self, items, today=DAY1, ratio=0.8) -> Plan:
        plan = plan_refresh(self.existing(), items, today=today, min_keep_ratio=ratio)
        self.apply(plan)
        return plan

    def by_symbol(self, symbol: str) -> Existing:
        [row] = [r for r in self.rows.values() if r.symbol == symbol and r.is_active]
        return row


FILE = [stock("FAKEA", "INE000A01011"), stock("FAKEB", "INE000B01012", "BE")]


# --- plan --------------------------------------------------------------------------------


def test_first_refresh_inserts_everything():
    table = Table()
    plan = table.refresh(FILE)
    assert (len(plan.inserts), plan.updated, plan.changes) == (2, 0, 2)
    assert table.by_symbol("FAKEB").series == "BE"


def test_second_refresh_of_the_same_file_changes_nothing():
    table = Table()
    table.refresh(FILE)
    again = table.refresh(FILE)
    assert again.changes == 0
    assert not (again.updates or again.inserts or again.deactivate or again.history or again.seen)


def test_a_new_day_only_moves_last_seen_and_counts_no_change():
    table = Table()
    table.refresh(FILE)
    plan = table.refresh(FILE, today=DAY2)
    assert plan.changes == 0 and len(plan.seen) == 2
    assert {r.last_seen_on for r in table.rows.values()} == {DAY2}


def test_changed_fields_are_updated():
    table = Table()
    table.refresh(FILE)
    changed = [FILE[0].model_copy(update={"series": "BE", "lot_size": 5}), FILE[1]]
    plan = table.refresh(changed)
    assert plan.updated == 1 and plan.changes == 1
    assert table.by_symbol("FAKEA").lot_size == 5


def test_missing_instruments_are_deactivated_and_come_back():
    table = Table()
    table.refresh(FILE + [stock("FAKEC", "INE000C01013")], ratio=0.5)
    plan = table.refresh(FILE, ratio=0.5)
    assert len(plan.deactivate) == 1
    gone = next(r for r in table.rows.values() if r.symbol == "FAKEC")
    assert not gone.is_active  # kept, only inactive
    back = table.refresh(FILE + [stock("FAKEC", "INE000C01013")], ratio=0.5)
    assert back.reactivated == 1 and len(back.deactivate) == 0
    assert table.by_symbol("FAKEC").id == gone.id


def test_a_symbol_change_writes_history():
    table = Table()
    table.refresh(FILE)
    renamed = [FILE[0].model_copy(update={"symbol": "FAKEANEW"}), FILE[1]]
    plan = table.refresh(renamed, today=DAY2)
    row = table.by_symbol("FAKEANEW")
    assert table.history == [(row.id, "FAKEA", "FAKEANEW", DAY2)]
    assert len(plan.history) == 1 and plan.updated == 1


def test_two_instruments_can_swap_symbols():
    table = Table()
    table.refresh(FILE)
    swapped = [
        FILE[0].model_copy(update={"symbol": "FAKEB"}),
        FILE[1].model_copy(update={"symbol": "FAKEA"}),
    ]
    plan = table.refresh(swapped)  # Table.apply asserts the unique index holds at the end
    assert len(plan.history) == 2 and len(plan.move_off) == 2


def test_a_symbol_can_be_reused_after_the_old_instrument_goes_inactive():
    table = Table()
    table.refresh(FILE + [stock("FAKEC", "INE000C01013")], ratio=0.5)
    table.refresh(FILE, ratio=0.5)  # FAKEC inactive
    reused = FILE + [stock("FAKEC", "INE000Z01019")]  # a new company takes the symbol
    plan = table.refresh(reused, ratio=0.5)
    assert len(plan.inserts) == 1
    rows = [r for r in table.rows.values() if r.symbol == "FAKEC"]
    assert sorted(r.is_active for r in rows) == [False, True]


def test_the_safety_valve_refuses_a_much_smaller_file():
    table = Table()
    many = [stock(f"FAKE{i:02d}", f"INE0000{i:02d}011") for i in range(10)]
    table.refresh(many)
    with pytest.raises(RefreshRefused, match="keeps 7 instruments, fewer than 0.8 x the 10"):
        table.refresh(many[:7])
    assert all(r.is_active for r in table.rows.values())  # nothing changed
    table.refresh(many[:8])  # exactly 0.8 x is allowed


def test_the_safety_valve_is_off_while_nothing_is_active():
    plan = Table().refresh(FILE[:1])
    assert len(plan.inserts) == 1


# --- run_refresh (command level, fake database) ------------------------------------------


class FakeConn:
    """ingestion_runs rows plus a no-op transaction; the instrument table is patched in."""

    def __init__(self):
        self.runs: dict[int, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO ingestion_runs"):
            run_id = len(self.runs) + 1
            self.runs[run_id] = {"status": "running", "params": json.loads(params[2])}
            return Result((run_id,))
        if sql.startswith("UPDATE ingestion_runs"):
            run = self.runs[params[-1]]
            run["rows_written"] = params[0]
            run["status"] = "failed" if "'failed'" in sql else "success"
            if run["status"] == "failed":
                run["error"] = params[1]
            return Result(None)
        raise AssertionError(f"unexpected SQL: {sql}")


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings, a fake connection and the in-memory table wired into the instruments module."""
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
        "ALERT_DRY_RUN": "true",
        "ALERT_EMAIL_FROM": "sender@example.invalid",
        "ALERT_EMAIL_TO": "owner@example.invalid",
        "ALERT_EMAIL_APP_PASSWORD": FAKE_APP_PW,
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, "instruments", console_stream=io.StringIO())
    conn, table = FakeConn(), Table()
    monkeypatch.setattr(instruments, "load_existing", lambda c: table.existing())
    monkeypatch.setattr(instruments, "apply_plan", lambda c, plan: table.apply(plan))
    # the process-wide alert sender, with these settings and no atexit hook
    monkeypatch.setattr(alerts, "_background", None)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    monkeypatch.setattr(alerts.atexit, "register", lambda fn: fn)
    return settings, conn, table, tmp_path


def objects(n: int) -> list[dict]:
    return [eq(f"FAKE{i:03d}", f"INE000{i:03d}011") for i in range(n)] + [index("FAKE 50")]


def test_refresh_from_a_file_records_a_successful_run(env):
    settings, conn, table, tmp = env
    path = write_gz(tmp, objects(5) + [{"segment": "NSE_FO"}, "junk"])
    summary = instruments.run_refresh(settings, file=path, today=DAY1, connect=lambda db: conn)
    assert summary.plan.changes == 6 and len(table.rows) == 6
    [run] = conn.runs.values()
    assert run == {
        "status": "success",
        "params": {"file": "fake.json.gz", "download": False},
        "rows_written": 6,
    }
    text = "\n".join(summary.lines())
    assert "inserted 6, updated 0, symbol changes 0, deactivated 0, reactivated 0" in text
    assert "EQ 5, BE 0, indices 1" in text
    assert "skipped by segment: NSE_FO 1; malformed: 1" in text
    again = instruments.run_refresh(settings, file=path, today=DAY1, connect=lambda db: conn)
    assert again.plan.changes == 0 and conn.runs[2]["rows_written"] == 0


def test_dry_run_changes_nothing_and_records_no_run(env, monkeypatch):
    settings, conn, table, tmp = env
    path = write_gz(tmp, objects(3))
    applied: list[Plan] = []
    monkeypatch.setattr(instruments, "apply_plan", lambda c, plan: applied.append(plan))

    class RollbackConn(FakeConn):
        rolled_back = False

        def __exit__(self, exc_type, *rest):
            if exc_type is instruments.psycopg.Rollback:
                RollbackConn.rolled_back = True
                return True
            return False

    rb = RollbackConn()
    summary = instruments.run_refresh(
        settings, file=path, dry_run=True, today=DAY1, connect=lambda db: rb
    )
    assert summary.lines()[0] == "DRY RUN (nothing saved): instruments refresh: 4 changes"
    assert len(applied) == 1 and RollbackConn.rolled_back
    assert rb.runs == {} and table.rows == {}


def test_safety_valve_refuses_alerts_and_marks_the_run_failed(env):
    settings, conn, table, tmp = env
    instruments.run_refresh(settings, file=write_gz(tmp, objects(20)), connect=lambda db: conn)
    tiny = write_gz(tmp, objects(2), name="tiny.json.gz")
    with pytest.raises(RefreshRefused):
        instruments.run_refresh(settings, file=tiny, connect=lambda db: conn)
    assert sum(r.is_active for r in table.rows.values()) == 21  # nothing changed
    failed = conn.runs[2]
    assert failed["status"] == "failed" and failed["rows_written"] == 0
    assert failed["error"].startswith("RefreshRefused: the file keeps 3 instruments")
    assert alerts.background_alerts().flush(5)
    lines = (settings.log_dir / log_file_name("instruments")).read_text().splitlines()
    [alert] = [json.loads(x) for x in lines if '"event": "alert"' in x]
    assert alert["status"] == "dry_run"
    assert alert["title"] == "Instrument refresh refused: file too small"


def test_download_path_saves_validators_only_after_success(env, monkeypatch):
    settings, conn, table, tmp = env
    from market_intel.sources import upstox_instruments as ui

    server = Server(gz_bytes(objects(3)))
    run = instruments.run_refresh(settings, client=server.client(), connect=lambda db: conn)
    assert run.plan.changes == 4
    assert (ui.raw_dir(settings.data_dir) / ui.STATE_FILE).exists()
    again = instruments.run_refresh(settings, client=server.client(), connect=lambda db: conn)
    assert again.not_modified
    assert again.lines() == ["instrument file not modified since the last refresh: nothing to do"]
    assert conn.runs[2] == {
        "status": "success",
        "params": {"file": None, "download": True},
        "rows_written": 0,
    }


def test_a_refused_download_does_not_save_validators(env):
    settings, conn, table, tmp = env
    from market_intel.sources import upstox_instruments as ui

    instruments.run_refresh(settings, file=write_gz(tmp, objects(20)), connect=lambda db: conn)
    server = Server(gz_bytes(objects(1)))
    with pytest.raises(RefreshRefused):
        instruments.run_refresh(settings, client=server.client(), connect=lambda db: conn)
    assert not (ui.raw_dir(settings.data_dir) / ui.STATE_FILE).exists()


def test_cli_exit_codes(env, capsys, monkeypatch):
    settings, conn, table, tmp = env
    monkeypatch.setattr(instruments, "get_settings", lambda: settings)
    monkeypatch.setattr(instruments, "configure_logging", lambda *a, **k: None)
    good = write_gz(tmp, objects(3))
    assert instruments.main(["refresh", "--file", str(good)], connect=lambda db: conn) == 0
    assert "instruments refresh: 4 changes" in capsys.readouterr().out
    assert instruments.main(["refresh", "--file", str(tmp / "nope.gz")]) == 2
    bad = tmp / "bad.json.gz"
    bad.write_bytes(b"not gzip")
    assert instruments.main(["refresh", "--file", str(bad)], connect=lambda db: conn) == 1
    assert "not gzip" in capsys.readouterr().err


# --- sector import -----------------------------------------------------------------------

HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"


def write_csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "fake_nifty500.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_sector_csv_is_read_with_a_bom_and_spaces(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text(
        "﻿" + HEADER + "Fake Alpha Ltd., Fake Industry ,FAKEA,EQ,INE000A01011\n\n",
        encoding="utf-8",
    )
    assert read_sector_csv(path) == [SectorRow("FAKEA", "EQ", "INE000A01011", "Fake Industry")]


@pytest.mark.parametrize(
    "header",
    [
        "Company,Industry,Symbol,Series,ISIN Code\n",
        "Company Name,Industry,Symbol,Series\n",
        "Company Name,Industry,Symbol,Series,ISIN Code,Extra\n",
        "",
    ],
)
def test_unexpected_headers_are_refused(tmp_path, header):
    with pytest.raises(SectorFileError, match="unexpected headers"):
        read_sector_csv(write_csv(tmp_path, "", header=header))


def test_a_short_row_is_refused(tmp_path):
    with pytest.raises(SectorFileError, match="row 2 has 3 columns"):
        read_sector_csv(write_csv(tmp_path, "Fake Alpha,Fake Industry,FAKEA\n"))


def test_an_oversized_csv_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(instruments, "SECTOR_FILE_MAX_BYTES", 10)
    with pytest.raises(SectorFileError, match="larger than 10 bytes"):
        read_sector_csv(write_csv(tmp_path, ""))


def test_sectors_match_by_isin_then_symbol_and_never_set_empty():
    table = [
        (1, "INE000A01011", "FAKEA", "EQ", None),
        (2, "INE000B01012", "FAKEB", "EQ", "Old Sector"),
        (3, "INE000C01013", "FAKEC", "EQ", "Same Sector"),
        (4, "INE000D01014", "FAKED", "BE", None),
        (5, "INE000E01015", "FAKEE", "EQ", "Kept Sector"),
    ]
    rows = [
        SectorRow("FAKEA", "EQ", "INE000A01011", "New Sector"),  # by ISIN
        SectorRow("FAKEB", "EQ", "INE999X01019", "Changed Sector"),  # ISIN differs: by symbol
        SectorRow("FAKEC", "EQ", "INE000C01013", "Same Sector"),  # unchanged
        SectorRow("FAKED", "EQ", "INE999Y01011", "Any Sector"),  # symbol is BE here: unmatched
        SectorRow("FAKEE", "EQ", "INE000E01015", ""),  # empty: never overwrites
        SectorRow("FAKEZ", "EQ", "INE999Z01013", "Other Sector"),  # unknown
    ]
    plan = plan_sectors(rows, table)
    assert plan.updates == [("New Sector", 1), ("Changed Sector", 2)]
    assert (plan.matched, plan.unmatched, plan.unchanged, plan.empty) == (4, 2, 1, 1)


def test_import_sectors_dry_run_rolls_back(tmp_path, env):
    settings, _, _, _ = env
    path = write_csv(tmp_path, "Fake Alpha,Fake Industry,FAKEA,EQ,INE000A01011\n")
    executed = []

    class SectorConn:
        rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *rest):
            if exc_type is instruments.psycopg.Rollback:
                SectorConn.rolled_back = True
                return True
            return False

        def transaction(self):
            return self

        def cursor(self):
            return self

        def execute(self, sql, params=()):
            return Rows([(1, "INE000A01011", "FAKEA", "EQ", None)])

        def executemany(self, sql, rows):
            executed.extend(rows)

    plan = instruments.run_import_sectors(
        settings, path, dry_run=True, connect=lambda db: SectorConn()
    )
    assert plan.updates == [("Fake Industry", 1)] and executed == [("Fake Industry", 1)]
    assert SectorConn.rolled_back
    assert plan.lines(True)[0] == "DRY RUN (nothing saved): sector import: matched 1, unmatched 0"


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows
