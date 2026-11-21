"""Part B (BSE scrip codes from Upstox's BSE file, by ISIN; BSE items match by code first) and
Part C (rule f: a source silent for 14 days; the block record). Fakes only."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from instrument_fakes import Server, eq, gz_bytes, write_gz

from market_intel import instruments
from market_intel.config import load_settings
from market_intel.instruments import BseRefused, plan_bse_codes
from market_intel.logging_setup import configure_logging
from market_intel.news import InstrumentIndex
from market_intel.source_health import SourceHealth
from market_intel.sources import upstox_instruments as ui

FAKE_PW = "fake-pass-222"
T0 = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)


def bse(isin: str, code: str, segment: str = "BSE_EQ") -> dict:
    return {
        "segment": segment,
        "name": "FAKE CO",
        "exchange": "BSE",
        "isin": isin,
        "instrument_type": "A",
        "instrument_key": f"{segment}|{isin}",
        "lot_size": 1,
        "exchange_token": code,
        "tick_size": 5.0,
        "trading_symbol": "FAKECO",
    }


# --- parsing the BSE file ----------------------------------------------------------------


def test_bse_codes_come_from_exchange_token_of_bse_eq_objects(tmp_path):
    path = write_gz(
        tmp_path,
        [
            bse("INE000A01011", "500001"),
            bse("INE000B01012", "500002"),
            bse("INE000C01013", "9xx"),  # not a 6-digit code
            bse("not-an-isin", "500004"),
            bse("INE000D01014", "500005", segment="BSE_FO"),
            bse("INE000E01015", "500006"),
            bse("INE000F01016", "500006"),  # the same code twice: ambiguous, dropped
            "junk",
        ],
    )
    codes = ui.parse_bse_codes(path)
    assert codes.by_isin == {"INE000A01011": "500001", "INE000B01012": "500002"}
    assert codes.other_segments == 1 and codes.malformed == 5


def test_a_bad_bse_file_is_refused(tmp_path):
    path = tmp_path / "bad.json.gz"
    path.write_bytes(b"not gzip")
    with pytest.raises(ui.InstrumentFileError):
        ui.parse_bse_codes(path)


def test_bse_download_has_its_own_file_and_validators(tmp_path):
    server = Server(gz_bytes([bse("INE000A01011", "500001")]))
    got = ui.download(
        "https://x.invalid/BSE.json.gz", tmp_path, T0.date(), client=server.client(), prefix="BSE"
    )
    assert got.path.name == "BSE-20261122.json.gz"
    ui.save_validators(tmp_path, "https://x.invalid/BSE.json.gz", got, "BSE")
    assert (ui.raw_dir(tmp_path) / "state-bse.json").exists()
    assert not (ui.raw_dir(tmp_path) / "state.json").exists()  # the NSE validators untouched
    again = ui.download(
        "https://x.invalid/BSE.json.gz", tmp_path, T0.date(), client=server.client(), prefix="BSE"
    )
    assert again.not_modified


# --- planning the codes ------------------------------------------------------------------


def test_codes_are_set_by_isin_moved_and_cleared():
    rows = [
        (1, "INE000A01011", None),  # gets a code
        (2, "INE000B01012", "500002"),  # unchanged
        (3, "INE000C01013", "500009"),  # its code moves to row 4
        (4, "INE000D01014", None),
        (5, "INE000E01015", None),  # not in the BSE file: stays without a code
    ]
    by_isin = {
        "INE000A01011": "500001",
        "INE000B01012": "500002",
        "INE000D01014": "500009",
        "INE000C01013": "500003",
    }
    plan = plan_bse_codes(rows, by_isin, min_keep_ratio=0.8)
    assert plan.clear == [3]  # cleared first, so 500009 can move to row 4
    assert plan.set_codes == [("500001", 1), ("500003", 3), ("500009", 4)]
    assert plan.mapped == 4


def test_the_bse_safety_valve_refuses_a_much_smaller_file():
    rows = [(i, f"INE0000{i:02d}011", f"5000{i:02d}") for i in range(10)]
    with pytest.raises(BseRefused, match="maps 1 stocks, fewer than 0.8 x the 10"):
        plan_bse_codes(rows, {"INE000000011": "500000"}, min_keep_ratio=0.8)


def test_bse_items_match_by_code_first_then_by_name():
    index = InstrumentIndex(
        [("FAKEA", "FAKE ALPHA LTD", "500001"), ("FAKEB", "FAKE BETA LTD", None)]
    )
    assert index.match("BSE", "Totally Different Name Ltd", "500001") == (["FAKEA"], "matched")
    assert index.match("BSE", "Fake Beta Limited", "599999") == (["FAKEB"], "matched")
    assert index.match("NSE", "Nothing", "500001") == ([], "unmatched")  # codes are BSE only


# --- the refresh's BSE step --------------------------------------------------------------


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    """ingestion_runs plus the BSE statements, against an in-memory instrument list."""

    def __init__(self, rows):
        self.rows = {r[0]: list(r) for r in rows}  # id -> [id, isin, bse_code]
        self.runs: dict[int, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO ingestion_runs"):
            self.runs[len(self.runs) + 1] = {"status": "running", "params": json.loads(params[2])}
            return Result([(len(self.runs),)])
        if sql.startswith("UPDATE ingestion_runs"):
            run = self.runs[params[-1]]
            run["rows_written"] = params[0]
            run["status"] = next(s for s in ("failed", "partial", "success") if f"'{s}'" in sql)
            if run["status"] != "success":
                run["error"] = params[1]
            return Result([])
        if sql.startswith("SELECT id, isin, bse_code"):
            return Result([tuple(r) for r in self.rows.values()])
        if sql.startswith("SELECT count(*) FROM instruments WHERE bse_code"):
            return Result([(sum(r[2] is not None for r in self.rows.values()),)])
        if sql.startswith("UPDATE instruments SET bse_code = NULL"):
            for row_id in params[0]:
                self.rows[row_id][2] = None
            return Result([])
        raise AssertionError(f"unexpected SQL: {sql}")

    def executemany(self, sql, rows):
        assert sql.startswith("UPDATE instruments SET bse_code = %s")
        for code, row_id in rows:
            self.rows[row_id][2] = code


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
        "ALERT_DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(key, value)
    s = load_settings()
    configure_logging(s, "instruments", console_stream=io.StringIO())
    return s


def nse_rows():
    return [(1, "INE000A01011", None), (2, "INE000B01012", None)]


def test_refresh_maps_bse_codes_after_the_nse_part(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(instruments, "load_existing", lambda c: [])
    monkeypatch.setattr(instruments, "apply_plan", lambda c, plan: None)
    conn = Conn(nse_rows())
    nse_file = write_gz(tmp_path, [eq("FAKEA", "INE000A01011")], name="nse.json.gz")
    bse_file = write_gz(tmp_path, [bse("INE000A01011", "500001")], name="bse.json.gz")
    summary = instruments.run_refresh(
        settings, file=nse_file, bse=True, bse_file=bse_file, connect=lambda db: conn
    )
    assert conn.rows[1][2] == "500001" and conn.rows[2][2] is None
    assert conn.runs[1]["status"] == "success" and conn.runs[1]["rows_written"] == 2  # 1 + 1
    assert "BSE codes: 1 instruments have a bse_code (set 1, cleared 0)" in "\n".join(
        summary.all_lines()
    )


def test_only_the_bse_file_failing_makes_the_run_partial(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(instruments, "load_existing", lambda c: [])
    monkeypatch.setattr(instruments, "apply_plan", lambda c, plan: None)
    conn = Conn(nse_rows())
    nse_file = write_gz(tmp_path, [eq("FAKEA", "INE000A01011")], name="nse.json.gz")
    bad = tmp_path / "bse.json.gz"
    bad.write_bytes(b"not gzip")
    summary = instruments.run_refresh(
        settings, file=nse_file, bse=True, bse_file=bad, connect=lambda db: conn
    )
    assert summary.plan.changes == 1  # the NSE part still counted
    assert conn.runs[1]["status"] == "partial"
    assert conn.runs[1]["error"].startswith("BSE codes failed: InstrumentFileError")
    assert "BSE codes: FAILED" in summary.all_lines()[-1]


def test_cli_skips_bse_with_file_only(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(instruments, "get_settings", lambda: settings)
    monkeypatch.setattr(instruments, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(instruments, "load_existing", lambda c: [])
    monkeypatch.setattr(instruments, "apply_plan", lambda c, plan: None)
    conn = Conn(nse_rows())
    nse_file = write_gz(tmp_path, [eq("FAKEA", "INE000A01011")], name="nse.json.gz")
    assert instruments.main(["refresh", "--file", str(nse_file)], connect=lambda db: conn) == 0
    assert "BSE codes: skipped (--file without --bse-file)" in capsys.readouterr().out


# --- rule f and blocks -------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def test_rule_f_needs_14_days_of_history_then_warns_on_silence(tmp_path):
    clock, alerts = Clock(), []
    sh = SourceHealth(tmp_path, now=clock, alert=lambda *a, **k: alerts.append(k))
    for day in range(14):
        clock.now = T0 + timedelta(days=day)
        assert sh.record_content("rss:quiet", items=0, malformed=0, without_time=0) == []
    clock.now = T0 + timedelta(days=14, minutes=1)
    assert sh.record_content("rss:quiet", items=0, malformed=0, without_time=0) == ["silent"]
    assert alerts[-1]["key"] == "source_health:rss:quiet:silent"
    clock.now += timedelta(hours=1)
    assert sh.record_content("rss:quiet", items=2, malformed=0, without_time=0) == []
    assert "silent" not in sh.state()["rss:quiet"]["problems"]


def test_rule_f_ignores_a_source_with_items_in_the_window(tmp_path):
    clock = Clock()
    sh = SourceHealth(tmp_path, now=clock, alert=lambda *a, **k: None)
    sh.record_content("rss:a", items=0, malformed=0, without_time=0)
    clock.now = T0 + timedelta(days=5)
    sh.record_content("rss:a", items=3, malformed=0, without_time=0)
    clock.now = T0 + timedelta(days=15)
    assert sh.record_content("rss:a", items=0, malformed=0, without_time=0) == []  # day 5 had items
    log = sh.state()["rss:a"]["content_log"]
    assert all(datetime.fromisoformat(t) >= T0 for t, _ in log) and len(log) <= 3


def test_a_block_alerts_once_and_a_success_clears_it(tmp_path):
    alerts = []
    sh = SourceHealth(
        tmp_path, now=Clock(), alert=lambda level, title, body, **k: alerts.append(title)
    )
    assert sh.record_block("google_news", "google_news blocked: HTTP 429") == ["blocked"]
    assert sh.record_block("google_news", "again") == []
    assert alerts == ["google_news blocked"]
    sh.record_content("google_news", items=3, malformed=0, without_time=0)
    assert "blocked" not in sh.state()["google_news"]["problems"]
