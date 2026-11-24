"""The daily candle loader (S1 part D) with an in-memory store, plus the universe import and the
exchange rematch (part E). Precedence, gaps (open / filled / unfillable), resume, partial and
failed runs, the yfinance fallback, and suspected split/bonus detection. No network."""

import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from market_intel import candles, instruments, upstox
from market_intel.candles import Fetchers, Inst, run_daily
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging
from market_intel.news import InstrumentIndex, rematch_exchange
from market_intel.sources import daily_candles as dc
from market_intel.sources.records import DailyCandle

FAKE_PW = "fake-pass-555"
NOW = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)  # 18:30 IST: today's candle is ready
NIFTY = Inst(1, "NIFTY", "INDEX", "NSE_INDEX|Nifty 50")
BANK = Inst(2, "BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank")
STOCK_A = Inst(10, "FAKEA", "EQ", "NSE_EQ|INE000A01011")
STOCK_B = Inst(11, "FAKEB", "EQ", "NSE_EQ|INE000B01012")
WEEKDAYS = [date(2026, 11, 2) + timedelta(days=i) for i in range(23)]
DAYS = [d for d in WEEKDAYS if d.weekday() < 5]  # 17 trading days up to 24 Nov


def candle(source, symbol, day, close=Decimal(100)):
    return DailyCandle(
        source=source,
        symbol=symbol,
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=10,
        price_basis="raw",
    )


class FakeStore:
    def __init__(self, universe):
        self.universe = universe
        self.rows: dict[tuple[int, date], DailyCandle] = {}
        self.gap_rows: dict[tuple[int, date], dict] = {}
        self.events: dict[tuple[int, date], Decimal] = {}
        self.next_gap = 1

    def instruments(self, universe, symbol):
        if symbol:
            return [i for i in self.universe if i.symbol == symbol]
        return [i for i in self.universe if i.id != NIFTY.id]

    def calendar_instrument(self):
        return NIFTY

    def stored_dates(self, inst_id):
        return sorted(d for i, d in self.rows if i == inst_id)

    def source_dates(self, inst_id, source):
        return sorted(d for (i, d), c in self.rows.items() if i == inst_id and c.source == source)

    def upsert(self, inst_id, rows):
        n = 0
        for c in rows:
            old = self.rows.get((inst_id, c.trade_date))
            if old is not None and old.source == "upstox" and c.source == "yfinance":
                continue  # never the reverse
            if old is not None and old == c:
                continue
            self.rows[(inst_id, c.trade_date)] = c
            n += 1
        return n

    def closes(self, inst_id, since):
        return [
            (d, c.close) for (i, d), c in sorted(self.rows.items()) if i == inst_id and d >= since
        ]

    def gaps(self, inst_id):
        return [
            (g["id"], a, g["end"], g["status"])
            for (i, a), g in sorted(self.gap_rows.items())
            if i == inst_id
        ]

    def save_gaps(self, inst_id, rows, filled, run_id):
        for g in self.gap_rows.values():
            if g["id"] in filled:
                g["status"], g["run"] = "filled", run_id
        for a, b, status in rows:
            g = self.gap_rows.setdefault((inst_id, a), {"id": self.next_gap})
            self.next_gap += g["id"] == self.next_gap
            g.update(end=b, status=status)

    def add_price_events(self, inst_id, events):
        new = [(d, r) for d, r in events if (inst_id, d) not in self.events]
        self.events.update({(inst_id, d): r for d, r in new})
        return len(new)

    def gap_counts(self, ids):
        out = {}
        for (i, _), g in self.gap_rows.items():
            if i in ids:
                out[g["status"]] = out.get(g["status"], 0) + 1
        return out

    def price_event_count(self, ids):
        return sum(1 for i, _ in self.events if i in ids)


class FakeUpstox:
    """Serves candles per symbol for the asked range; can drop days or fail."""

    def __init__(self, days=DAYS, missing=None, fail=(), expire=False, closes=None):
        self.days, self.missing, self.fail = days, missing or {}, set(fail)
        self.expire, self.closes = expire, closes or {}
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, client, key, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.expire:
            raise upstox.TokenExpired("expired")
        if symbol in self.fail:
            raise upstox.UpstoxError("HTTP 500: -")
        out = dc.CandleBatch(requests=1)
        for d in self.days:
            if start <= d <= end and d not in self.missing.get(symbol, ()):
                out.candles.append(
                    candle("upstox", symbol, d, self.closes.get((symbol, d), Decimal(100)))
                )
        return out


class FakeYahoo:
    def __init__(self, days=DAYS, fail=(), missing=None):
        self.days, self.fail, self.missing = days, set(fail), missing or {}
        self.calls: list[tuple[str, date, date]] = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            raise dc.CandleSourceError("yfinance: blocked")
        out = dc.CandleBatch(requests=1)
        out.candles = [
            candle("yfinance", symbol, d, Decimal(99))
            for d in self.days
            if start <= d <= end and d not in self.missing.get(symbol, ())
        ]
        return out


class RunsConn:
    def __init__(self):
        self.runs: dict[int, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO ingestion_runs"):
            self.runs[len(self.runs) + 1] = {"job": params[0], "source": params[1]}
            return type("R", (), {"fetchone": lambda s: (len(self.runs),)})()
        run = self.runs[params[-1]]
        run["rows_written"] = params[0]
        run["status"] = next(s for s in ("failed", "partial", "success") if f"'{s}'" in sql)
        if run["status"] != "success":
            run["error"] = params[1]
        return None


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


def load(settings, store, up, yahoo, conn=None, **kw):
    fetchers = Fetchers(
        upstox=None if up is None else object(),
        yahoo=yahoo,
        upstox_fetch=up.fetch if up else dc.fetch_upstox,
    )
    from market_intel.source_health import SourceHealth

    health = SourceHealth(settings.data_dir, alert=lambda *a, **k: None)
    return run_daily(
        settings,
        store=store,
        fetchers=fetchers,
        connect=lambda db: conn or RunsConn(),
        health=health,
        now=lambda: NOW,
        start_from=kw.pop("start_from", DAYS[0]),
        **kw,
    )


def test_first_load_then_an_incremental_rerun_writes_nothing(settings):
    store, up, yahoo, conn = (
        FakeStore([NIFTY, BANK, STOCK_A]),
        FakeUpstox(),
        FakeYahoo(),
        RunsConn(),
    )
    first = load(settings, store, up, yahoo, conn)
    assert first.status == "success" and first.rows == 3 * len(DAYS)
    assert first.by_source["upstox"] == 3 * len(DAYS) and not yahoo.calls
    assert up.calls[0][0] == "NIFTY"  # the calendar is loaded first
    assert conn.runs[1] == {
        "job": "candles_daily",
        "source": "upstox",
        "rows_written": 51,
        "status": "success",
    }
    again = load(settings, store, up, yahoo, conn, start_from=None)
    assert again.rows == 0 and again.resumed == 2  # finished instruments are not asked again


def test_upstox_failure_falls_back_to_yfinance_for_stocks_only(settings):
    store = FakeStore([NIFTY, BANK, STOCK_A])
    up, yahoo = FakeUpstox(fail={"FAKEA", "BANKNIFTY"}), FakeYahoo()
    conn = RunsConn()
    summary = load(settings, store, up, yahoo, conn)
    assert store.rows[(STOCK_A.id, DAYS[0])].source == "yfinance"
    assert summary.failed == 1 and summary.status == "partial"  # the index had no backup
    assert conn.runs[1]["status"] == "partial" and "BANKNIFTY" in conn.runs[1]["error"]
    assert [c[0] for c in yahoo.calls] == ["FAKEA"]


def test_upstox_replaces_yfinance_never_the_reverse(settings):
    store = FakeStore([NIFTY, STOCK_A])
    store.upsert(STOCK_A.id, [candle("yfinance", "FAKEA", DAYS[0], Decimal(99))])
    load(settings, store, FakeUpstox(), FakeYahoo())
    assert store.rows[(STOCK_A.id, DAYS[0])].source == "upstox"
    assert store.upsert(STOCK_A.id, [candle("yfinance", "FAKEA", DAYS[0], Decimal(1))]) == 0
    assert store.rows[(STOCK_A.id, DAYS[0])].close == Decimal(100)


def test_no_token_means_stocks_from_yfinance_and_indices_fail(settings):
    with pytest.raises(candles.AllFailed):  # the index can't load and the stock's backup fails
        load(settings, FakeStore([NIFTY, STOCK_A]), None, FakeYahoo(fail={"FAKEA"}))
    summary = load(settings, FakeStore([NIFTY, STOCK_A]), None, FakeYahoo())
    assert summary.status == "partial" and summary.failed == 1  # NIFTY: Upstox only
    assert summary.by_source == {"yfinance": len(DAYS)}


def test_an_expired_token_stops_upstox_for_the_rest_of_the_run(settings):
    store, up, yahoo = FakeStore([NIFTY, STOCK_A, STOCK_B]), FakeUpstox(expire=True), FakeYahoo()
    summary = load(settings, store, up, yahoo)
    assert len(up.calls) == 1  # only the first try; then no more Upstox calls
    assert {c[0] for c in yahoo.calls} == {"FAKEA", "FAKEB"} and summary.failed == 1


def test_missing_days_are_retried_with_yfinance_then_gaps(settings):
    hole, lost = DAYS[5], DAYS[9]
    store = FakeStore([NIFTY, STOCK_A])
    up = FakeUpstox(missing={"FAKEA": {hole, lost}})
    yahoo = FakeYahoo(missing={"FAKEA": {lost}})  # yfinance has one of the two days
    summary = load(settings, store, up, yahoo)
    assert store.rows[(STOCK_A.id, hole)].source == "yfinance"
    assert (STOCK_A.id, lost) not in store.rows
    assert len(yahoo.calls) == 1  # one request spanning the missing days
    [gap] = store.gap_rows.values()
    assert (gap["status"], gap["end"]) == ("unfillable", lost)  # both sources tried
    assert summary.gaps == {"unfillable": 1}


def test_gaps_stay_open_without_upstox_and_fill_later(settings):
    hole = DAYS[4]
    store = FakeStore([NIFTY, STOCK_A])
    load(settings, store, FakeUpstox(missing={"FAKEA": {hole}}), FakeYahoo(fail={"FAKEA"}))
    [gap] = store.gap_rows.values()
    assert gap["status"] == "unfillable"
    store2 = FakeStore([NIFTY, STOCK_A])
    store2.upsert(NIFTY.id, [candle("upstox", "NIFTY", d) for d in DAYS])
    store2.upsert(STOCK_A.id, [candle("yfinance", "FAKEA", d) for d in DAYS if d != hole])
    (settings.data_dir / "state" / "candles_progress.json").unlink()  # a new backfill
    load(settings, store2, None, FakeYahoo(fail={"FAKEA"}), start_from=None)
    [open_gap] = store2.gap_rows.values()
    assert open_gap["status"] == "open"  # Upstox wasn't tried: may still fill it
    (settings.data_dir / "state" / "candles_progress.json").unlink()
    load(settings, store2, FakeUpstox(), FakeYahoo(), start_from=None)
    assert open_gap["status"] == "filled" and (STOCK_A.id, hole) in store2.rows


def test_an_interrupted_backfill_resumes_where_it_stopped(settings):
    store = FakeStore([NIFTY, STOCK_A, STOCK_B])
    up = FakeUpstox(fail={"FAKEB"})
    load(settings, store, up, FakeYahoo(fail={"FAKEB"}))
    progress = json.loads((settings.data_dir / "state" / "candles_progress.json").read_text())
    assert set(progress["done"]) == {"1", "10"}
    up2 = FakeUpstox()
    second = load(settings, store, up2, FakeYahoo())
    assert [c[0] for c in up2.calls] == ["FAKEB"] and second.resumed == 1  # NIFTY up to date


def test_dry_run_writes_nothing(settings):
    store = FakeStore([NIFTY, STOCK_A])
    summary = run_daily(
        settings,
        store=store,
        fetchers=Fetchers(upstox=object(), yahoo=FakeYahoo(), upstox_fetch=FakeUpstox().fetch),
        dry_run=True,
        now=lambda: NOW,
        start_from=DAYS[0],
    )
    assert summary.lines()[0].startswith("DRY RUN (nothing saved)") and not store.rows
    assert summary.by_source["upstox"] == 2 * len(DAYS)
    assert not (settings.data_dir / "state" / "candles_progress.json").exists()


# --- split / bonus -----------------------------------------------------------------------


def test_suspected_splits_need_a_common_ratio_and_a_still_market():
    d = DAYS
    closes = [
        (d[0], Decimal(1000)),
        (d[1], Decimal(502)),
        (d[2], Decimal(500)),
        (d[3], Decimal(330)),
        (d[4], Decimal(331)),
        (d[5], Decimal(160)),
    ]
    market = {d[i]: Decimal(20000) for i in range(6)}
    market[d[5]] = Decimal(17000)  # a 15% market fall: not a split
    events = candles.suspected_splits(closes, market)
    assert [e[0] for e in events] == [d[1], d[3]]
    assert events[0][1] == Decimal("0.502000")


def test_the_loader_records_price_events_without_changing_prices(settings):
    split_day = DAYS[8]
    closes = {("FAKEA", d): Decimal(1000) if d < split_day else Decimal(500) for d in DAYS}
    store = FakeStore([NIFTY, STOCK_A])
    summary = load(settings, store, FakeUpstox(closes=closes), FakeYahoo())
    assert store.events == {(STOCK_A.id, split_day): Decimal("0.500000")}
    assert store.rows[(STOCK_A.id, split_day)].close == Decimal(500)  # unchanged
    assert summary.events_new == 1 and summary.events_total == 1


def test_last_trading_day():
    assert candles.last_trading_day(NOW) == date(2026, 11, 24)
    assert candles.last_trading_day(NOW - timedelta(hours=2)) == date(2026, 11, 23)  # 16:30 IST


# --- universe import and rematch ---------------------------------------------------------


def test_the_sector_import_records_the_nifty500_universe(tmp_path, settings):
    path = tmp_path / "n500.csv"
    path.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Fake Alpha,Fake Industry,FAKEA,EQ,INE000A01011\n"
        "Fake Zed,Fake Industry,FAKEZ,EQ,INE999Z01013\n",
        encoding="utf-8",
    )
    statements = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def transaction(self):
            return self

        def cursor(self):
            return self

        def execute(self, sql, params=()):
            statements.append((sql, params))
            return type(
                "R", (), {"fetchall": lambda s: [(7, "INE000A01011", "FAKEA", "EQ", None)]}
            )()

        def executemany(self, sql, rows):
            statements.append((sql, rows))

    plan = instruments.run_import_sectors(settings, path, connect=lambda db: Conn())
    assert plan.matched_ids == [7] and "universe nifty500: 1 members" in plan.lines(False)[-1]
    sqls = [s for s, _ in statements]
    assert "DELETE FROM universe_members WHERE universe = 'nifty500'" in sqls
    [(insert, params)] = [
        (s, p) for s, p in statements if s.startswith("INSERT INTO universe_members")
    ]
    assert params[0] == [7]


class Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt, projection=None):
        return [d for d in self.docs if d["kind"] == flt["kind"]]

    def update_one(self, flt, update):
        next(d for d in self.docs if d["_id"] == flt["_id"]).update(update["$set"])


def test_rematch_uses_the_bse_code_first_and_changes_only_symbols():
    docs = [
        {
            "_id": 1,
            "kind": "exchange",
            "symbols": [],
            "title": "t",
            "exchange_meta": {
                "exchange": "BSE",
                "company_raw": "Odd Name Ltd",
                "symbol_raw": "500001",
            },
        },
        {
            "_id": 2,
            "kind": "exchange",
            "symbols": ["FAKEB"],
            "exchange_meta": {
                "exchange": "NSE",
                "company_raw": "Fake Beta Limited",
                "symbol_raw": None,
            },
        },
        {
            "_id": 3,
            "kind": "exchange",
            "symbols": ["WRONG"],
            "exchange_meta": {"exchange": "NSE", "company_raw": "Unknown Ltd", "symbol_raw": None},
        },
    ]
    index = InstrumentIndex(
        [("FAKEA", "FAKE ALPHA LTD", "500001"), ("FAKEB", "FAKE BETA LTD", None)]
    )
    coll = Coll(docs)
    dry = rematch_exchange(coll, index, dry_run=True)
    assert (dry.changed, dry.unchanged, dry.matched, dry.unmatched) == (2, 1, 2, 1)
    assert docs[0]["symbols"] == []
    rematch_exchange(coll, index)
    assert [d["symbols"] for d in docs] == [["FAKEA"], ["FAKEB"], []]
    assert docs[0]["title"] == "t"
    assert rematch_exchange(coll, index).changed == 0
