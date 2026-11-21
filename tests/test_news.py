"""The news command: dedupe keeps the first document, minimum interval, Retry-After, not
modified, success / partial / failed runs, dry run, symbol matching and status. Fakes only:
synthetic feeds (httpx MockTransport), an in-memory collection and ingestion_runs table."""

import io
import json
from datetime import timedelta

import httpx
import pytest
from news_fakes import T0, FeedServer, item, many, rss
from pymongo.errors import DuplicateKeyError

from market_intel import news
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging
from market_intel.news import InstrumentIndex, normalise_company, run_fetch, status_lines
from market_intel.source_health import SourceHealth

FAKE_PW = "fake-pass-321"
URL_A, URL_B, URL_N = (
    "https://a.example.invalid/feed",
    "https://b.example.invalid/feed",
    "https://nse.example.invalid/feed",
)
FEEDS = f"""
[[feed]]
name = "fake_a"
url = "{URL_A}"
kind = "news"
publisher = "Fake A"

[[feed]]
name = "fake_b"
url = "{URL_B}"
kind = "news"
publisher = "Fake B"
min_interval_minutes = 30

[[feed]]
name = "fake_nse"
url = "{URL_N}"
kind = "exchange"
publisher = "NSE"

[[feed]]
name = "fake_off"
url = "https://off.example.invalid/feed"
kind = "news"
publisher = "Off"
enabled = false
"""


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    def find(self, flt, projection=None):
        wanted = set(flt["url_hash"]["$in"])
        return [{"url_hash": d["url_hash"]} for d in self.docs if d["url_hash"] in wanted]

    def insert_one(self, doc):
        if any(d["url_hash"] == doc["url_hash"] for d in self.docs):
            raise DuplicateKeyError("dup")
        self.docs.append(dict(doc))


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class RunsConn:
    """The ingestion_runs table in memory (the helper's INSERT and UPDATE statements)."""

    def __init__(self):
        self.runs: dict[int, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO ingestion_runs"):
            run_id = len(self.runs) + 1
            self.runs[run_id] = {"status": "running", "job": params[0], "source": params[1]}
            return Result((run_id,))
        if sql.startswith("UPDATE ingestion_runs"):
            run = self.runs[params[-1]]
            run["rows_written"] = params[0]
            for status in ("failed", "partial", "success"):
                if f"'{status}'" in sql:
                    run["status"] = status
            if run["status"] in ("failed", "partial"):
                run["error"] = params[1]
            return Result(None)
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(FEEDS, encoding="utf-8")
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "NEWS_FEEDS_FILE": str(feeds),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, "news", console_stream=io.StringIO())
    return settings


class Clock:
    def __init__(self):
        self.now = T0 + timedelta(minutes=30)

    def __call__(self):
        return self.now


INDEX = InstrumentIndex(
    [("FAKEALPHA", "FAKE ALPHA LIMITED"), ("FAKEBETA", "Fake Beta & Sons Ltd"),
     ("FAKEG1", "FAKE GAMMA LTD"), ("FAKEG2", "Fake Gamma Limited")]
)  # fmt: skip


def nse_feed() -> bytes:
    return rss(
        item("Fake Alpha Ltd.", "https://nse.example.invalid/1.pdf", "x |SUBJECT: Fake outcome"),
        item("FAKE BETA AND SONS LIMITED", "https://nse.example.invalid/2.pdf", "y"),
        item("Fake Gamma Limited", "https://nse.example.invalid/3.pdf", "z"),  # ambiguous
        item("Unknown Fake Co Limited", "https://nse.example.invalid/4.pdf", "w"),
    )


def runner(settings, server, clock, coll, conn, **kw):
    alerts = []
    health = SourceHealth(settings.data_dir, now=clock, alert=lambda *a, **k: alerts.append(k))
    return run_fetch(
        settings,
        client=server.client(),
        collection=coll,
        index=INDEX,
        connect=lambda db: conn,
        health=health,
        now=clock,
        **kw,
    )


def test_fetch_stores_new_items_and_a_second_fetch_keeps_the_first_document(env):
    server = FeedServer({URL_A: many(3), URL_B: many(2, "b"), URL_N: nse_feed()})
    clock, coll, conn = Clock(), FakeCollection(), RunsConn()
    first = runner(env, server, clock, coll, conn)
    assert first.status == "success" and first.new == 9 and len(coll.docs) == 9
    assert conn.runs[1] == {
        "status": "success",
        "job": "news_fetch",
        "source": "rss",
        "rows_written": 9,
    }
    original = dict(coll.docs[0])
    # the same items again later, with a changed title: the first document stays unchanged
    clock.now += timedelta(minutes=40)
    server.routes[URL_A] = rss(*(item(f"Changed {i}", f"https://news.example.invalid/a{i}")
                                 for i in range(3)))  # fmt: skip
    second = runner(env, server, clock, coll, conn, force=True)
    feed_a = next(f for f in second.feeds if f.name == "fake_a")
    assert (feed_a.new, feed_a.duplicate) == (0, 3)
    assert coll.docs[0] == original and len(coll.docs) == 9
    assert conn.runs[2]["rows_written"] == 0
    assert {f.name: f.outcome for f in second.feeds}["fake_nse"] == "not_modified"
    assert all(str(r.url) in (URL_A, URL_B, URL_N) for r in server.requests)  # no item links


def test_documents_have_the_timing_fields(env):
    server = FeedServer({URL_A: rss(item(pub=T0)), URL_B: rss(item(pub=None, link="https://b.invalid/1")),
                         URL_N: rss()})  # fmt: skip
    clock, coll = Clock(), FakeCollection()
    runner(env, server, clock, coll, RunsConn())
    by_feed = {d["feed_name"]: d for d in coll.docs}
    a, b = by_feed["fake_a"], by_feed["fake_b"]
    assert (a["published_at"], a["fetched_at"], a["available_at"]) == (T0, clock.now, clock.now)
    assert a["published_at_source"] == "feed" and a["kind"] == "news" and a["symbols"] == []
    assert b["published_at"] == b["fetched_at"] == b["available_at"] == clock.now
    assert b["published_at_source"] == "fetched"


def test_exchange_items_are_linked_only_on_a_clear_match(env):
    server = FeedServer({URL_A: rss(), URL_B: rss(), URL_N: nse_feed()})
    coll = FakeCollection()
    summary = runner(env, server, Clock(), coll, RunsConn())
    symbols = {d["exchange_meta"]["company_raw"]: d["symbols"] for d in coll.docs}
    assert symbols == {
        "Fake Alpha Ltd.": ["FAKEALPHA"],
        "FAKE BETA AND SONS LIMITED": ["FAKEBETA"],
        "Fake Gamma Limited": [],  # two active stocks share the name: ambiguous
        "Unknown Fake Co Limited": [],
    }
    nse = next(f for f in summary.feeds if f.name == "fake_nse")
    assert (nse.matched, nse.ambiguous, nse.unmatched) == (2, 1, 1)
    assert "symbols matched 2, ambiguous 1, unmatched 1" in "\n".join(summary.lines())


def test_name_normalisation_and_symbol_match():
    assert normalise_company("Fake Beta & Sons Ltd.") == "FAKE BETA AND SONS"
    assert normalise_company("FAKE BETA AND SONS LIMITED") == "FAKE BETA AND SONS"
    assert normalise_company("Fake (India) Pvt-Ltd") == "FAKE INDIA PVT"
    assert INDEX.match("NSE", None, "fakealpha") == (["FAKEALPHA"], "matched")
    assert INDEX.match("BSE", "Nothing", "500001") == ([], "unmatched")


def test_the_minimum_interval_skips_a_feed_unless_forced(env):
    server = FeedServer({URL_A: many(1), URL_B: many(1, "b"), URL_N: rss()})
    clock, coll, conn = Clock(), FakeCollection(), RunsConn()
    runner(env, server, clock, coll, conn)
    clock.now += timedelta(minutes=15)  # fake_a (10 min) is due; fake_b (30 min) is not
    again = runner(env, server, clock, coll, conn)
    outcomes = {f.name: f.outcome for f in again.feeds}
    assert outcomes["fake_b"] == "skipped:interval" and outcomes["fake_a"] == "not_modified"
    clock.now += timedelta(minutes=1)
    everything = runner(env, server, clock, coll, conn)
    assert {f.outcome for f in everything.feeds} == {"skipped:interval"}
    assert everything.status == "success" and conn.runs[3]["rows_written"] == 0
    forced = runner(env, server, clock, coll, conn, force=True)
    assert "skipped:interval" not in {f.outcome for f in forced.feeds}


def test_retry_after_is_respected_even_with_force(env):
    server = FeedServer(
        {URL_A: httpx.Response(429, headers={"Retry-After": "3600"}), URL_B: many(1), URL_N: rss()}
    )
    clock, conn = Clock(), RunsConn()
    first = runner(env, server, clock, FakeCollection(), conn)
    assert first.status == "partial"
    assert conn.runs[1]["status"] == "partial"
    assert conn.runs[1]["error"] == "1 of 3 feeds failed: fake_a"
    clock.now += timedelta(minutes=20)
    again = runner(env, server, clock, FakeCollection(), conn, force=True)
    assert {f.name: f.outcome for f in again.feeds}["fake_a"] == "skipped:retry_after"
    asked = [r for r in server.requests if str(r.url) == URL_A]
    assert len(asked) == 1


def test_all_attempted_feeds_failing_marks_the_run_failed(env):
    server = FeedServer({})  # every feed answers 404
    conn = RunsConn()
    with pytest.raises(news.AllFeedsFailed, match="all 3 attempted feeds failed") as caught:
        runner(env, server, Clock(), FakeCollection(), conn)
    assert conn.runs[1]["status"] == "failed"
    assert conn.runs[1]["error"].startswith("AllFeedsFailed: all 3 attempted feeds failed")
    assert "failed 3" in caught.value.summary.lines()[0]


def test_failures_reach_source_health(env):
    server = FeedServer({URL_A: httpx.Response(503), URL_B: many(1), URL_N: rss()})
    clock, alerts = Clock(), []
    health = SourceHealth(env.data_dir, now=clock, alert=lambda *a, **k: alerts.append(k))
    for _ in range(3):
        run_fetch(env, client=server.client(), collection=FakeCollection(), index=INDEX,
                  connect=lambda db: RunsConn(), health=health, now=clock, force=True)  # fmt: skip
        clock.now += timedelta(minutes=11)
    assert [a["key"] for a in alerts] == ["source_health:rss:fake_a:failures"]
    # fake_b answered 304 the 2nd and 3rd time: only content fetches are counted
    assert health.state()["rss:fake_b"]["counts"] == [1]
    assert health.state()["rss:fake_b"]["consecutive_failures"] == 0


def test_dry_run_saves_nothing(env):
    server = FeedServer({URL_A: many(2), URL_B: many(1, "b"), URL_N: rss()})

    def no_db(db):
        raise AssertionError("a dry run must not touch the database")

    summary = run_fetch(env, client=server.client(), dry_run=True, connect=no_db, now=Clock())
    assert summary.lines()[0].startswith("DRY RUN (nothing saved): news fetch: success; new 3")
    assert not (env.data_dir / "state").exists()  # no feed state, no source health


def test_a_single_feed_and_an_unknown_name(env):
    server = FeedServer({URL_B: many(1)})
    summary = runner(env, server, Clock(), FakeCollection(), RunsConn(), feed_name="fake_b")
    assert [f.name for f in summary.feeds] == ["fake_b"]
    with pytest.raises(news.FeedConfigError, match="no feed named"):
        runner(env, server, Clock(), FakeCollection(), RunsConn(), feed_name="nope")


def test_feed_state_holds_validators_and_times_only(env):
    server = FeedServer({URL_A: many(2), URL_B: many(1, "b"), URL_N: rss()})
    runner(env, server, Clock(), FakeCollection(), RunsConn())
    state = json.loads((env.data_dir / "state" / "news_feeds.json").read_text(encoding="utf-8"))
    assert set(state) == {"fake_a", "fake_b", "fake_nse"}
    assert set(state["fake_a"]) == {
        "etag",
        "last_modified",
        "last_fetch_at",
        "last_content_at",
        "last_items",
    }
    assert "headline" not in json.dumps(state).lower()


def test_status_shows_each_feed(env):
    server = FeedServer({URL_A: many(2), URL_B: httpx.Response(503), URL_N: rss()})
    runner(env, server, Clock(), FakeCollection(), RunsConn())
    lines = status_lines(env, counts={"fake_a": 2}, tz=None)
    assert lines[0].split()[:4] == ["feed", "on", "last", "fetch"]
    row = {line.split()[0]: line for line in lines[1:]}
    assert set(row) == {"fake_a", "fake_b", "fake_nse", "fake_off"}
    assert row["fake_a"].split()[-2:] == ["2", "-"]  # 2 documents, no problems
    assert " 1 " in row["fake_b"] and row["fake_off"].split()[1] == "no"
