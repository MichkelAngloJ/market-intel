"""Upstox quirks seen in the first real backfill (2026-11-24): a day returned twice (00:00 and
09:15 candles) keeps the 00:00 candle; instruments Upstox rejects as invalid keys (UDAPI1021)
are reported as not served, not as failures. Fakes only."""

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from market_intel import upstox
from market_intel.candles import Fetchers, Inst, run_daily
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging
from market_intel.source_health import SourceHealth
from market_intel.sources import daily_candles as dc

FAKE_PW = "fake-pass-666"


def test_a_duplicate_day_keeps_the_midnight_candle():
    batch = dc.parse_upstox(
        {
            "status": "success",
            "data": {
                "candles": [
                    ["2018-03-01T09:15:00+05:30", 10, 12, 9, 11, 0, 0],
                    ["2018-03-01T00:00:00+05:30", 10, 12, 9, 11.5, 0, 0],
                    ["2018-03-02T00:00:00+05:30", 11, 13, 10, 12, 0, 0],
                    ["2018-03-02T17:30:00+05:30", 11, 13, 10, 12.5, 0, 0],
                    ["2018-03-05T09:15:00+05:30", 12, 13, 11, 12, 0, 0],
                ]
            },
        },
        "FAKEIDX",
    )
    closes = {c.trade_date: c.close for c in batch.candles}
    assert closes == {
        date(2018, 3, 1): Decimal("11.5000"),
        date(2018, 3, 2): Decimal("12.0000"),
        date(2018, 3, 5): Decimal("12.0000"),  # only candle of its day, whatever its time
    }
    assert batch.duplicates == 2 and batch.malformed == 0


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
    configure_logging(s, "candles", console_stream=io.StringIO())
    return s


def test_an_instrument_upstox_does_not_serve_is_not_a_failure(settings):
    from test_candles_loader import NIFTY, FakeStore, FakeUpstox, FakeYahoo, RunsConn

    bond = Inst(3, "FAKEBOND-APR31", "INDEX", "NSE_INDEX|FAKEBOND-APR31")
    up = FakeUpstox()

    def fetch(client, key, symbol, start, end):
        if symbol == bond.symbol:
            raise upstox.UpstoxError("HTTP 400: UDAPI1021")
        return up.fetch(client, key, symbol, start, end)

    conn = RunsConn()
    summary = run_daily(
        settings,
        store=FakeStore([NIFTY, bond]),
        fetchers=Fetchers(upstox=object(), yahoo=FakeYahoo(), upstox_fetch=fetch),
        connect=lambda db: conn,
        health=SourceHealth(settings.data_dir, alert=lambda *a, **k: None),
        now=lambda: datetime(2026, 11, 24, 13, 0, tzinfo=UTC),
        start_from=date(2026, 11, 2),
    )
    assert (summary.status, summary.failed, summary.unsupported) == ("success", 0, 1)
    assert conn.runs[1]["status"] == "success"
    assert "not served by Upstox 1" in summary.lines()[0]


def test_the_sources_health_check_covers_the_candle_sources(settings):
    from market_intel import health

    sh = SourceHealth(settings.data_dir, alert=lambda *a, **k: None)
    sh.record_content("upstox_candles", items=10, malformed=0, without_time=0)
    for _ in range(3):
        sh.record_failure("yfinance_candles", "yfinance: blocked")
    result = health.check_sources(health.Context(lambda: settings))
    assert result.status == "WARN" and "yfinance_candles: failures" in result.message
    assert result.details["sources"] == 2
