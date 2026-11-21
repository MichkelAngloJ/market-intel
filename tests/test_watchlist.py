"""Watchlist rules: stocks only (EQ/BE, active), max 50, BE risk note, note length, and the
command output. Fakes only (the real SQL runs in tests/integration/test_instruments_db.py)."""

import io
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from market_intel import watchlist
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging, log_file_name
from market_intel.watchlist import BE_NOTE, MAX_ITEMS, Candidate, WatchlistError, choose

FAKE_PW = "fake-pass-987"
STOCK = Candidate(id=1, segment="EQ", series="EQ")
BE_STOCK = Candidate(id=2, segment="EQ", series="BE")
INDEX = Candidate(id=3, segment="INDEX", series=None)


def test_an_active_stock_can_be_added():
    assert choose("FAKEA", [STOCK], count=0, listed=set()) == STOCK


def test_indices_are_refused():
    with pytest.raises(WatchlistError, match="FAKE 50 is an index"):
        choose("FAKE 50", [INDEX], count=0, listed=set())


def test_unknown_or_other_series_are_refused():
    with pytest.raises(WatchlistError, match="not an active NSE stock"):
        choose("FAKEX", [], count=0, listed=set())
    with pytest.raises(WatchlistError, match="not an active NSE stock"):
        choose("FAKEX", [Candidate(4, "EQ", "SM")], count=0, listed=set())


def test_the_stock_wins_when_an_index_shares_the_symbol():
    assert choose("FAKEA", [INDEX, STOCK], count=0, listed=set()) == STOCK


def test_a_stock_already_listed_is_refused():
    with pytest.raises(WatchlistError, match="already on the watchlist"):
        choose("FAKEA", [STOCK], count=1, listed={1})


def test_the_51st_stock_is_refused():
    assert choose("FAKEA", [STOCK], count=MAX_ITEMS - 1, listed=set()) == STOCK
    with pytest.raises(WatchlistError, match="the watchlist is full \\(50 stocks\\)"):
        choose("FAKEA", [STOCK], count=MAX_ITEMS, listed=set())


def test_symbols_are_normalised_and_checked():
    assert watchlist.normalise("  fakea ") == "FAKEA"
    with pytest.raises(WatchlistError):
        watchlist.normalise("   ")


# --- add / remove / list against a fake connection -----------------------------------------


class Result:
    def __init__(self, rows=None, rowcount=0):
        self.rows, self.rowcount = rows or [], rowcount

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, instruments, listed=()):
        self.instruments = instruments  # symbol -> [(id, segment, series)]
        self.listed = list(listed)
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if sql.startswith("LOCK TABLE watchlist"):
            return Result()
        if sql.startswith("SELECT id, segment, series FROM instruments"):
            return Result(self.instruments.get(params[0], []))
        if sql.startswith("SELECT instrument_id FROM watchlist"):
            return Result([(i,) for i in self.listed])
        if sql.startswith("INSERT INTO watchlist"):
            self.listed.append(params[0])
            return Result()
        if sql.startswith("DELETE FROM watchlist"):
            return Result(rowcount=1 if params[0] == "FAKEA" else 0)
        if sql.startswith(watchlist.LIST_SQL[:30]):
            added = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)  # 22 Nov in IST
            return Result(
                [
                    ("FAKEA", "Fake Alpha Ltd", "EQ", "Fake Sector", added, "a note", True),
                    ("FAKEB", "Fake Beta Ltd", "BE", None, added, None, False),
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def test_adding_a_be_stock_prints_the_risk_note():
    conn = FakeConn({"FAKEB": [(2, "EQ", "BE")]})
    lines = watchlist.add(conn, "fakeb")
    assert lines == [f"added FAKEB (1 of {MAX_ITEMS})", f"note: {BE_NOTE}"]
    assert conn.sql[0].startswith("LOCK TABLE watchlist")  # the 50 check can't race
    assert BE_NOTE == "series BE: trade-to-trade, often under exchange surveillance; higher risk"


def test_adding_an_eq_stock_has_no_note():
    assert watchlist.add(FakeConn({"FAKEA": [(1, "EQ", "EQ")]}), "FAKEA") == [
        f"added FAKEA (1 of {MAX_ITEMS})"
    ]


def test_add_counts_the_existing_rows_against_the_limit():
    conn = FakeConn({"FAKEA": [(1, "EQ", "EQ")]}, listed=range(100, 100 + MAX_ITEMS))
    with pytest.raises(WatchlistError, match="full"):
        watchlist.add(conn, "FAKEA")
    assert 1 not in conn.listed


def test_a_note_longer_than_200_characters_is_refused():
    with pytest.raises(WatchlistError, match="longer than 200"):
        watchlist.add(FakeConn({}), "FAKEA", "x" * 201)


def test_remove_needs_a_listed_symbol():
    assert watchlist.remove(FakeConn({}), "fakea") == ["removed FAKEA"]
    with pytest.raises(WatchlistError, match="FAKEZ is not on the watchlist"):
        watchlist.remove(FakeConn({}), "FAKEZ")


def test_list_shows_symbol_name_series_sector_date_and_note():
    lines = watchlist.list_lines(FakeConn({}), ZoneInfo("Asia/Kolkata"))
    assert lines[0] == f"watchlist: 2 of {MAX_ITEMS}"
    assert lines[2].split()[:4] == ["FAKEA", "EQ", "2026-11-22", "Fake"]
    assert lines[2].endswith("Fake Alpha Ltd / a note")
    assert "FAKEB" in lines[3] and lines[3].endswith("Fake Beta Ltd (INACTIVE)")


def test_an_empty_list_says_so():
    class Empty(FakeConn):
        def execute(self, sql, params=()):
            return Result()

    assert watchlist.list_lines(Empty({}), ZoneInfo("UTC")) == ["the watchlist is empty (0 of 50)"]


def test_cli_prints_errors_and_logs_no_symbols(tmp_path, monkeypatch, capsys):
    for key, value in {
        "APP_PROFILE": "full",
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    monkeypatch.setattr(watchlist, "get_settings", lambda: settings)
    monkeypatch.setattr(
        watchlist,
        "configure_logging",
        lambda s, c: configure_logging(s, c, console_stream=io.StringIO()),
    )
    conn = FakeConn({"FAKEA": [(1, "EQ", "EQ")], "FAKE 50": [(3, "INDEX", None)]})
    assert watchlist.main(["add", "FAKEA", "--note", "n"], connect=lambda db: conn) == 0
    assert "added FAKEA" in capsys.readouterr().out
    assert watchlist.main(["add", "FAKE 50"], connect=lambda db: conn) == 1
    assert "is an index" in capsys.readouterr().err
    log_text = (settings.log_dir / log_file_name("watchlist")).read_text(encoding="utf-8")
    assert "watchlist_added" in log_text and "FAKEA" not in log_text
