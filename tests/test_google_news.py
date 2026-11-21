"""Part D (1.2b): per-company news from Google News RSS. Clean names and query encoding, the
confidence rule, targets (watchlist + recent filers, cap, exclusions), the per-company interval
and the process-wide rate, every block signal (stop, pause until the next IST day, one alert),
$addToSet for a shared article, and the run status. Synthetic XML/HTML only, no network."""

import io
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest
from pymongo.errors import DuplicateKeyError

from market_intel import news
from market_intel.config import load_settings
from market_intel.logging_setup import configure_logging
from market_intel.mongo.schemas import NEWS_ITEMS_SCHEMA
from market_intel.news import Target, run_company_fetch, select_targets, store_company_items
from market_intel.rate_limit import RateLimiter, Window
from market_intel.source_health import SourceHealth
from market_intel.sources import google_news as gn

T0 = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)  # 11:30 IST
FAKE_PW = "fake-pass-333"
BASE = "https://news.example.invalid/rss/search"


# --- names, queries, confidence ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "clean"),
    [
        ("FAKE ALPHA LIMITED", "FAKE ALPHA"),
        ("The Fake  Beta Co. Ltd.", "Fake Beta Co."),
        ("FAKE (INDIA) LTD", "FAKE (INDIA)"),
        ("  FAKEONE   LTD  ", "FAKEONE"),
    ],
)
def test_clean_names(name, clean):
    assert gn.clean_name(name) == clean


def test_the_query_is_the_exact_name_for_two_days_in_india_english():
    url = gn.query_url(BASE, "Fake Alpha & Sons")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == BASE
    query = parse_qs(parts.query)
    assert query == {
        "q": ['"Fake Alpha & Sons" when:2d'],
        "hl": ["en-IN"],
        "gl": ["IN"],
        "ceid": ["IN:en"],
    }
    assert " " not in url and "&Sons" not in url  # encoded


def test_confidence_rule():
    assert gn.match_confidence("FAKE ALPHA", "Fake Alpha wins an order", None) == "high"
    assert gn.match_confidence("FAKE ALPHA", "A deal", "<summary> fake  alpha shares") == "high"
    assert gn.match_confidence("FAKE ALPHA", "Markets today", "Nothing here") == "low"
    assert gn.match_confidence("FAKEONE", "FakeOne wins an order", None) == "low"  # one word


def test_the_block_pause_ends_at_the_next_ist_midnight():
    assert gn.next_ist_midnight(T0) == datetime(2026, 11, 22, 18, 30, tzinfo=UTC)
    late = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)  # 00:30 IST on the 23rd
    assert gn.next_ist_midnight(late) == datetime(2026, 11, 23, 18, 30, tzinfo=UTC)


# --- targets -----------------------------------------------------------------------------


def test_targets_are_the_watchlist_then_capped_recent_filers():
    watchlist = [("FAKEA", "FAKE ALPHA LTD"), ("FAKEB", "FAKE BETA LTD")]
    filers = ["FAKEC", "FAKEA", "FAKED", "FAKEC", "FAKEX", "FAKEE"]  # newest first
    names = {"FAKEC": "FAKE C LTD", "FAKED": "FAKE D LTD", "FAKEE": "FAKE E LTD"}
    targets = select_targets(watchlist, filers, names, cap=2)
    assert [(t.symbol, t.reason) for t in targets] == [
        ("FAKEA", "watchlist"),
        ("FAKEB", "watchlist"),
        ("FAKEC", "filer"),
        ("FAKED", "filer"),
    ]
    assert select_targets([], filers, names, cap=0) == []


def test_recent_filers_come_from_matched_exchange_items_of_the_last_day():
    class Cursor(list):
        def sort(self, key, direction):
            assert (key, direction) == ("available_at", -1)
            return self

    class Coll:
        def find(self, flt, projection):
            assert flt["kind"] == "exchange" and flt["symbols.0"] == {"$exists": True}
            assert flt["available_at"]["$gte"] == T0 - timedelta(hours=24)
            return Cursor([{"symbols": ["FAKEB"]}, {"symbols": ["FAKEA", "FAKEB"]}])

    assert news.recent_filer_symbols(Coll(), T0) == ["FAKEB", "FAKEA"]


# --- fetching: blocks ---------------------------------------------------------------------


def feed(*titles: str, link_prefix: str = "https://news.google.invalid/rss/articles/") -> bytes:
    items = "".join(
        f"<item><title>{escape(t)}</title><link>{link_prefix}{i}-{abs(hash(t))}</link>"
        f"<guid isPermaLink='false'>{i}</guid><pubDate>Sun, 22 Nov 2026 05:00:00 GMT</pubDate>"
        f"<description>&lt;a href='x'&gt;{escape(t)}&lt;/a&gt;</description>"
        f"<source url='https://pub.invalid'>Fake Times</source></item>"
        for i, t in enumerate(titles)
    )
    head = '<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>'
    return f"{head}{items}</channel></rss>".encode()


def limiter():
    # never waits: the rate itself is tested in test_one_request_per_min_seconds_across_the_process
    return RateLimiter("test", [Window(10_000, 1)], headroom=1.0)


BLOCKS = {
    "403": lambda r: httpx.Response(403),
    "429": lambda r: httpx.Response(429, headers={"Retry-After": "60"}),
    "503": lambda r: httpx.Response(503),
    "html": lambda r: httpx.Response(
        200, headers={"Content-Type": "text/html"}, content=b"<!doctype html><html>Hi</html>"
    ),
    "html-as-xml": lambda r: httpx.Response(
        200, headers={"Content-Type": "application/xml"}, content=b"<html><body>x</body></html>"
    ),
    "consent": lambda r: httpx.Response(
        302, headers={"Location": "https://consent.google.invalid/x"}
    ),
    "sorry": lambda r: httpx.Response(302, headers={"Location": "https://g.invalid/sorry/index"}),
    "not-a-feed": lambda r: httpx.Response(200, content=b"plain words, no feed"),
}


@pytest.mark.parametrize("kind", sorted(BLOCKS))
def test_each_block_signal_raises_google_blocked(kind):
    client = httpx.Client(transport=httpx.MockTransport(BLOCKS[kind]))
    with pytest.raises(gn.GoogleBlocked):
        body = gn.fetch(client, BASE, limiter=limiter(), max_bytes=100_000)
        gn.parse(body, symbol="FAKEA", clean="FAKE ALPHA", feed_url=BASE, fetched_at=T0)


def test_other_failures_are_company_errors_not_blocks():
    for response in (
        httpx.Response(404),
        httpx.Response(301, headers={"Location": "https://x.invalid/y"}),
    ):
        client = httpx.Client(transport=httpx.MockTransport(lambda r, resp=response: resp))
        with pytest.raises(gn.CompanyFetchError):
            gn.fetch(client, BASE, limiter=limiter(), max_bytes=100_000)


def test_requests_carry_the_honest_agent_and_redirects_are_not_followed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=feed("Fake Alpha news"))

    gn.fetch(gn.make_client() if False else httpx.Client(transport=httpx.MockTransport(handler)),
             BASE, limiter=limiter(), max_bytes=100_000)  # fmt: skip
    agent = seen[0].headers["User-Agent"]
    assert agent.startswith("market-intel/") and "Mozilla" not in agent
    assert gn.make_client().follow_redirects is False


def test_parsed_items_are_company_news_with_publisher_and_confidence():
    company = gn.parse(
        feed("Fake Alpha wins order", "Markets today"),
        symbol="FAKEA",
        clean="FAKE ALPHA",
        feed_url=BASE,
        fetched_at=T0 + timedelta(minutes=5),
    )
    first, second = (r.to_document() for r in company.records)
    assert (first.kind, first.feed_name, first.source, first.publisher) == (
        "company_news", "google_news", "google_news", "Fake Times",
    )  # fmt: skip
    assert first.symbols == ["FAKEA"] and first.url.startswith("https://news.google.invalid/")
    assert (first.match_confidence, second.match_confidence) == ("high", "low")
    assert first.published_at == T0 - timedelta(hours=1) and first.available_at == T0 + timedelta(
        minutes=5
    )
    assert "<" not in (first.summary or "")


# --- storing -----------------------------------------------------------------------------


class Coll:
    def __init__(self):
        self.docs: list[dict] = []

    def find_one(self, flt, projection=None):
        return next((d for d in self.docs if d["url_hash"] == flt["url_hash"]), None)

    def insert_one(self, doc):
        if self.find_one({"url_hash": doc["url_hash"]}):
            raise DuplicateKeyError("dup")
        self.docs.append(json.loads(json.dumps(doc, default=str)))

    def update_one(self, flt, update):
        doc = self.find_one(flt)
        sym = update["$addToSet"]["symbols"]
        assert set(update) == {"$addToSet"}  # nothing else changes
        if sym in doc["symbols"]:
            return type("R", (), {"modified_count": 0})()
        doc["symbols"].append(sym)
        return type("R", (), {"modified_count": 1})()


def test_a_shared_article_gains_the_other_symbol_only():
    coll = Coll()
    docs_a = [r.to_document().to_document() for r in gn.parse(
        feed("Fake Alpha and Fake Beta merge"), symbol="FAKEA", clean="FAKE ALPHA",
        feed_url=BASE, fetched_at=T0).records]  # fmt: skip
    docs_b = [r.to_document().to_document() for r in gn.parse(
        feed("Fake Alpha and Fake Beta merge"), symbol="FAKEB", clean="FAKE BETA",
        feed_url=BASE + "?b", fetched_at=T0 + timedelta(hours=1)).records]  # fmt: skip
    assert store_company_items(coll, docs_a, "FAKEA") == (1, 0, 0)
    before = dict(coll.docs[0])
    assert store_company_items(coll, docs_b, "FAKEB") == (0, 1, 0)
    assert store_company_items(coll, docs_b, "FAKEB") == (0, 0, 1)
    after = coll.docs[0]
    assert after["symbols"] == ["FAKEA", "FAKEB"]
    assert {k: v for k, v in after.items() if k != "symbols"} == {
        k: v for k, v in before.items() if k != "symbols"
    }


# --- the command -------------------------------------------------------------------------


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


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
            return Result((len(self.runs),))
        run = self.runs[params[-1]]
        run["rows_written"] = params[0]
        run["status"] = next(s for s in ("failed", "partial", "success") if f"'{s}'" in sql)
        if run["status"] != "success":
            run["error"] = params[1]
        return Result(None)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
        "GOOGLE_NEWS_RSS_URL": BASE,
    }.items():
        monkeypatch.setenv(key, value)
    s = load_settings()
    configure_logging(s, "news", console_stream=io.StringIO())
    return s


TARGETS = [
    Target("FAKEA", "FAKE ALPHA LTD", "watchlist"),
    Target("FAKEB", "FAKE BETA LTD", "filer"),
    Target("FAKEC", "FAKE GAMMA LTD", "filer"),
]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Google:
    """Answers per company (by the q parameter); records every request."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, request):
        self.requests.append(request)
        q = parse_qs(urlsplit(str(request.url)).query)["q"][0]
        for name, answer in self.answers.items():
            if name in q:
                return answer(request) if callable(answer) else httpx.Response(200, content=answer)
        return httpx.Response(200, content=feed())

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def run(settings, google, clock, coll=None, conn=None, alerts=None, **kw):
    alerts = alerts if alerts is not None else []
    health = SourceHealth(settings.data_dir, now=clock, alert=lambda *a, **k: alerts.append(a[1]))
    return run_company_fetch(
        settings,
        client=google.client(),
        collection=coll or Coll(),
        targets=kw.pop("targets", TARGETS),
        connect=lambda db: conn or RunsConn(),
        health=health,
        limiter=kw.pop("limiter", limiter()),
        now=clock,
        **kw,
    )


def test_a_round_fetches_each_company_and_records_a_run(settings):
    google = Google({"FAKE ALPHA": feed("Fake Alpha wins order"), "FAKE BETA": feed("Markets")})
    conn, coll = RunsConn(), Coll()
    summary = run(settings, google, Clock(), coll=coll, conn=conn)
    assert (summary.status, summary.requests, summary.new) == ("success", 3, 2)
    assert (summary.watchlist, summary.filers, summary.high, summary.low) == (1, 2, 1, 1)
    assert conn.runs[1] == {
        "job": "company_news",
        "source": "google_news",
        "rows_written": 2,
        "status": "success",
    }
    assert all("/rss/search" in str(r.url) for r in google.requests)  # never an article link
    lines = "\n".join(summary.lines())
    assert "targets 3 (watchlist 1, filers 2); requests 3" in lines and "blocked no" in lines


def test_each_company_is_fetched_at_most_once_per_interval(settings):
    google, clock = Google({}), Clock()
    run(settings, google, clock)
    clock.now += timedelta(minutes=30)
    second = run(settings, google, clock)
    assert (second.requests, second.skipped) == (0, 3)
    forced = run(settings, google, clock, force=True)
    assert forced.requests == 3
    clock.now += timedelta(minutes=61)
    assert run(settings, google, clock).requests == 3


def test_one_request_per_min_seconds_across_the_process():
    now, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    lim = RateLimiter("g", [Window(1, 10)], headroom=1.0, clock=lambda: now[0], sleep=sleep)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=feed()))
    )
    for _ in range(3):
        gn.fetch(client, BASE, limiter=lim, max_bytes=100_000)
    assert sleeps == [10.0, 10.0]
    assert gn.process_limiter(10) is gn.process_limiter(10)  # one shared limiter


@pytest.mark.parametrize("kind", ["429", "consent", "html"])
def test_a_block_stops_the_round_pauses_until_the_next_ist_day_and_alerts_once(settings, kind):
    google = Google({"FAKE ALPHA": feed("Fake Alpha news"), "FAKE BETA": BLOCKS[kind]})
    conn, clock, alerts = RunsConn(), Clock(), []
    summary = run(settings, google, clock, conn=conn, alerts=alerts)
    assert summary.blocked and summary.requests == 2  # FAKE GAMMA was never asked
    assert summary.status == "partial" and conn.runs[1]["status"] == "partial"
    assert conn.runs[1]["error"].startswith("Google blocked us; round stopped.")
    assert alerts == ["google_news blocked"]
    state = json.loads((settings.data_dir / "state" / "google_news.json").read_text())
    assert state["blocked_until"] == "2026-11-22T18:30:00+00:00"
    clock.now += timedelta(hours=2)
    paused = run(settings, google, clock, force=True, alerts=alerts)  # --force never overrides
    assert paused.requests == 0 and paused.paused_until == "2026-11-22T18:30:00+00:00"
    assert len(google.requests) == 2 and alerts == ["google_news blocked"]
    clock.now = datetime(2026, 11, 22, 18, 31, tzinfo=UTC)  # the next IST day
    assert run(settings, google, clock, targets=TARGETS[2:], alerts=alerts).requests == 1


def test_a_block_before_anything_was_fetched_fails_the_run(settings):
    google = Google({"FAKE ALPHA": BLOCKS["403"]})
    conn = RunsConn()
    with pytest.raises(news.CompanyFetchFailed, match="Google blocked us") as caught:
        run(settings, google, Clock(), conn=conn)
    assert conn.runs[1]["status"] == "failed" and caught.value.summary.requests == 1


def test_some_companies_failing_is_partial(settings):
    google = Google({"FAKE BETA": lambda r: httpx.Response(404)})
    conn = RunsConn()
    summary = run(settings, google, Clock(), conn=conn)
    assert summary.status == "partial" and summary.failed == 1 and not summary.blocked
    assert conn.runs[1]["error"] == "1 of 3 company requests failed"


def test_no_targets_is_a_quiet_success(settings):
    conn = RunsConn()
    summary = run(settings, Google({}), Clock(), conn=conn, targets=[])
    assert summary.status == "success" and summary.requests == 0
    assert conn.runs[1]["rows_written"] == 0


def test_dry_run_writes_nothing(settings):
    def no_db(db):
        raise AssertionError("a dry run must not touch Postgres")

    summary = run_company_fetch(
        settings, client=Google({}).client(), collection=Coll(), targets=TARGETS[:1],
        connect=no_db, limiter=limiter(), now=Clock(), dry_run=True,
    )  # fmt: skip
    assert summary.lines()[0].startswith("DRY RUN (nothing saved): company news: success")
    assert not (settings.data_dir / "state" / "google_news.json").exists()


def test_validator_mirror_for_kind_and_match_confidence():
    props = NEWS_ITEMS_SCHEMA["properties"]
    assert "company_news" in props["kind"]["enum"]
    assert props["match_confidence"]["enum"] == ["high", "low", None]
    assert "match_confidence" not in NEWS_ITEMS_SCHEMA["required"]
