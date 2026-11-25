"""1-minute candles (S4i): month ranges, the regular-session filter, thin days, resumable
progress and the loader's request plan. No database, no Upstox (fakes only)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from market_intel import intraday_candles as ic
from market_intel import upstox
from market_intel.config import ConfigError


def payload(*rows):
    return {"status": "success", "data": {"candles": [list(r) for r in rows]}}


def bar(day: str, hhmm: str, o=10.0, h=11.0, lo=9.0, c=10.5, v=100, oi=0):
    return (f"{day}T{hhmm}:00+05:30", o, h, lo, c, v, oi)


def test_months_are_clipped_and_cover_the_range():
    got = list(ic.months(date(2022, 1, 15), date(2022, 3, 10)))
    assert got == [
        (date(2022, 1, 15), date(2022, 1, 31)),
        (date(2022, 2, 1), date(2022, 2, 28)),
        (date(2022, 3, 1), date(2022, 3, 10)),
    ]
    assert list(ic.months(date(2023, 12, 5), date(2024, 1, 2)))[-1] == (
        date(2024, 1, 1),
        date(2024, 1, 2),
    )


def test_only_regular_session_bar_starts_are_kept_and_counted():
    batch = ic.parse_minutes(
        payload(
            bar("2024-03-04", "09:14"),
            bar("2024-03-04", "09:15"),
            bar("2024-03-04", "15:29"),
            bar("2024-03-04", "15:30"),
            bar("2024-03-04", "17:00"),
        )
    )
    local = batch.frame["ts"].dt.tz_convert(ic.IST).dt.strftime("%H:%M").tolist()
    assert local == ["09:15", "15:29"]
    assert batch.dropped_session == 3
    assert str(batch.frame["ts"].dt.tz) == "UTC"


def test_special_session_days_are_dropped():
    batch = ic.parse_minutes(
        payload(bar("2024-01-20", "10:00"), bar("2024-01-22", "10:00")), {date(2024, 1, 20)}
    )
    assert len(batch.frame) == 1
    assert batch.dropped_session == 1


def test_invalid_ohlc_is_dropped_and_duplicates_collapse():
    batch = ic.parse_minutes(
        payload(
            bar("2024-03-04", "10:00", h=9.5),  # high below close
            bar("2024-03-04", "10:01", lo=None),
            bar("2024-03-04", "10:02"),
            bar("2024-03-04", "10:02", c=10.7),
        )
    )
    assert batch.dropped_invalid == 2
    assert len(batch.frame) == 1
    assert batch.frame["close"].iloc[0] == 10.5


def test_bad_responses_are_upstox_errors():
    with pytest.raises(upstox.UpstoxError):
        ic.parse_minutes({"status": "error"})
    with pytest.raises(upstox.UpstoxError):
        ic.parse_minutes({"status": "success", "data": {}})
    assert ic.parse_minutes(payload()).frame.empty


def test_a_day_under_300_bars_is_thin():
    start = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST
    full = [start + timedelta(minutes=m) for m in range(375)]
    thin = [start + timedelta(days=1, minutes=m) for m in range(299)]
    frame = pd.DataFrame({"ts": pd.to_datetime(full + thin, utc=True)})
    assert ic.thin_days(frame) == {date(2024, 3, 5): 299}
    assert ic.thin_days(frame.iloc[:0]) == {}


def test_progress_survives_a_restart(tmp_path):
    p = ic.Progress(tmp_path)
    assert not p.has(7, date(2022, 1, 1))
    p.mark(7, date(2022, 1, 1))
    again = ic.Progress(tmp_path)
    assert again.has(7, date(2022, 1, 1))
    assert not again.has(8, date(2022, 1, 1))


def test_every_mapped_sector_index_is_an_nse_index_name():
    assert ic.SECTOR_INDEX["Financial Services"] == "NSE_INDEX|Nifty Fin Service"
    assert all(v.startswith("NSE_INDEX|") for v in ic.SECTOR_INDEX.values())


class FakeStore:
    def __init__(self, targets, last=None):
        self._targets = targets
        self._last = last or {}
        self.upserts = []
        self.thin = []

    def targets(self, universe, symbol):
        return [t for t in self._targets if symbol is None or t.symbol == symbol]

    def last_day(self, inst_id):
        return self._last.get(inst_id)

    def upsert(self, inst_id, frame):
        self.upserts.append((inst_id, len(frame)))
        return len(frame)

    def save_thin_days(self, inst_id, days):
        self.thin.append((inst_id, days))

    def thin_day_count(self, ids):
        return sum(len(d) for _, d in self.thin)


class FakeUpstox:
    def __init__(self, fail=()):
        self.paths = []
        self.fail = set(fail)

    def get(self, path):
        self.paths.append(path)
        if any(f in path for f in self.fail):
            raise upstox.UpstoxError("HTTP 500")
        frm = path.rsplit("/", 1)[1]
        return payload(bar(frm, "09:15"), bar(frm, "09:16"), bar(frm, "08:00"))


class Settings:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path


@pytest.fixture
def fake_loader(monkeypatch):
    targets = [
        ic.Target(1, "ZZA", "NSE_EQ|INE000000001", "stock"),
        ic.Target(2, "NIFTY", "NSE_INDEX|Nifty 50", "market"),
    ]
    store = FakeStore(targets, last={2: date(2024, 2, 20)})
    monkeypatch.setattr(ic, "IntradayStore", lambda conn: store)
    monkeypatch.setattr("market_intel.candles.special_sessions", lambda conn, **kw: [])
    return store


def test_dry_run_asks_one_month_per_request_from_to_then_from(tmp_path, fake_loader):
    client = FakeUpstox()
    now = datetime(2024, 3, 6, 12, 0, tzinfo=UTC)
    s = ic.run_intraday(Settings(tmp_path), None, start=date(2024, 1, 1), dry_run=True,
                        client=client, now=lambda: now)  # fmt: skip
    first = [p for p in client.paths if "INE000000001" in p]
    assert first[0].endswith("/minutes/1/2024-01-31/2024-01-01")
    assert "NSE_EQ%7CINE000000001" in first[0]
    assert len(first) == 3  # Jan, Feb, Mar
    nifty = [p for p in client.paths if "Nifty" in p]
    assert nifty[0].endswith("/2024-02-29/2024-02-21")  # incremental after the last stored day
    assert s.requests == 5 and s.rows == 0 and fake_loader.upserts == []
    assert s.dropped_session == 5 and s.bars == 10
    assert "DRY RUN" in s.lines()[0]


def test_failed_months_are_listed_and_the_run_continues(tmp_path, fake_loader):
    client = FakeUpstox(fail={"/2024-02-29/2024-02-01"})
    now = datetime(2024, 3, 6, 12, 0, tzinfo=UTC)
    s = ic.run_intraday(Settings(tmp_path), None, start=date(2024, 1, 1), dry_run=True,
                        client=client, now=lambda: now)  # fmt: skip
    assert s.failed == 1 and s.done == 1
    assert s.errors == ["ZZA 2024-02: HTTP 500"]


def test_unknown_symbol_is_a_config_error(tmp_path, fake_loader):
    with pytest.raises(ConfigError):
        ic.run_intraday(Settings(tmp_path), None, symbol="NOPE", dry_run=True, client=FakeUpstox())


def test_progress_file_replace_retries_a_briefly_locked_file(tmp_path, monkeypatch):
    calls = []
    real = ic.os.replace

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("Access is denied")
        real(src, dst)

    monkeypatch.setattr(ic.os, "replace", flaky)
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    ic.replace_file(src, dst, sleep=lambda s: None)
    assert dst.read_text(encoding="utf-8") == "{}" and len(calls) == 3
    src.write_text("{}", encoding="utf-8")
    calls.clear()
    monkeypatch.setattr(ic.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        ic.replace_file(src, dst, attempts=2, sleep=lambda s: None)
