"""news_items after mongo_setup, as the app logins: the new fields are accepted, the validator
rejects an item without available_at, a fetch from a FAKE feed (httpx MockTransport, no
network) writes and then dedupes, and a partial run is recorded in ingestion_runs.

Everything uses fake feed names starting with "zz_test_" and is deleted afterwards, so this
also runs safely against a real local database. Runs in CI compose-smoke (RUN_INTEGRATION=1).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import WriteError

from market_intel.config import get_settings
from market_intel.db import mongo, postgres
from market_intel.mongo.models import NewsItem
from market_intel.news import InstrumentIndex, run_fetch
from market_intel.source_health import SourceHealth

pytestmark = pytest.mark.integration

VALIDATION_FAILED = 121
NOW = datetime.now(UTC).replace(microsecond=0)
URL_OK, URL_BAD = "https://zz-ok.example.invalid/rss", "https://zz-bad.example.invalid/rss"
FEEDS = f"""
[[feed]]
name = "zz_test_ok"
url = "{URL_OK}"
kind = "exchange"
publisher = "NSE"

[[feed]]
name = "zz_test_bad"
url = "{URL_BAD}"
kind = "news"
publisher = "Fake"
"""


def feed_body() -> bytes:
    items = "".join(
        f"<item><title>Zz Fake Co {i} Limited</title>"
        f"<link>https://zz-ok.example.invalid/{ObjectId()}.pdf</link>"
        f"<description>x |SUBJECT: Fake subject {i}</description>"
        f"<pubDate>{(NOW - timedelta(hours=1)).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
        "</item>"
        for i in range(3)
    )
    head = '<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>'
    return f"{head}{items}</channel></rss>".encode()


@pytest.fixture
def app_db() -> Iterator[Database]:
    db = get_settings().require("database")
    with mongo.client(db) as client:
        database = client[db.mongodb_db]
        database.news_items.delete_many({"feed_name": {"$regex": "^zz_test_"}})
        yield database
        database.news_items.delete_many({"feed_name": {"$regex": "^zz_test_"}})


@pytest.fixture
def run_ids() -> Iterator[list[int]]:
    ids: list[int] = []
    yield ids
    if ids:
        with postgres.connect(get_settings().require("database")) as conn:
            conn.execute("DELETE FROM ingestion_runs WHERE id = ANY(%s)", (ids,))


def demo(**overrides) -> NewsItem:
    fields = dict(
        source="rss:zz_test_ok",
        feed_url=URL_OK,
        url=f"https://zz-ok.example.invalid/{ObjectId()}",
        title="ZZ integration test",
        published_at=NOW - timedelta(minutes=10),
        fetched_at=NOW,
        kind="exchange",
        publisher="NSE",
        feed_name="zz_test_ok",
        exchange_meta={"exchange": "NSE", "company_raw": "Zz Fake Co Limited"},
    )
    return NewsItem(**{**fields, **overrides})


def test_app_login_inserts_an_item_with_the_new_fields(app_db):
    item = demo()
    app_db.news_items.insert_one(item.to_document())
    found = app_db.news_items.find_one({"url_hash": item.url_hash})
    assert found["available_at"] == NOW and found["published_at_source"] == "feed"
    assert found["exchange_meta"]["company_raw"] == "Zz Fake Co Limited"


def test_validator_rejects_an_item_without_available_at(app_db):
    doc = demo().to_document()
    del doc["available_at"]
    with pytest.raises(WriteError) as exc:
        app_db.news_items.insert_one(doc)
    assert exc.value.code == VALIDATION_FAILED


def test_validator_rejects_unknown_exchange_meta_fields(app_db):
    doc = demo().to_document()
    doc["exchange_meta"]["price"] = 1
    with pytest.raises(WriteError) as exc:
        app_db.news_items.insert_one(doc)
    assert exc.value.code == VALIDATION_FAILED


def settings_for(tmp_path):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(FEEDS, encoding="utf-8")
    return get_settings().model_copy(update={"news_feeds_file": feeds, "data_dir": tmp_path})


def test_fetch_writes_then_dedupes_and_records_a_partial_run(app_db, run_ids, tmp_path):
    body = feed_body()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == URL_OK:
            return httpx.Response(200, stream=httpx.ByteStream(body))
        return httpx.Response(503)

    settings = settings_for(tmp_path)
    health = SourceHealth(tmp_path, alert=lambda *a, **k: None)
    index = InstrumentIndex([("ZZFAKE0", "ZZ FAKE CO 0 LTD")])

    def fetch():
        client = httpx.Client(transport=httpx.MockTransport(handler))
        summary = run_fetch(settings, client=client, index=index, health=health, force=True)
        run_ids.append(summary.run_id)
        return summary

    first = fetch()
    assert first.status == "partial" and first.new == 3
    stored = list(app_db.news_items.find({"feed_name": "zz_test_ok"}))
    assert len(stored) == 3
    assert {tuple(d["symbols"]) for d in stored} == {("ZZFAKE0",), ()}
    second = fetch()
    assert (
        second.new == 0 and next(f for f in second.feeds if f.name == "zz_test_ok").duplicate == 3
    )
    assert app_db.news_items.count_documents({"feed_name": "zz_test_ok"}) == 3
    with postgres.connect(get_settings().require("database")) as conn:
        rows = conn.execute(
            "SELECT status, rows_written, error FROM ingestion_runs WHERE id = ANY(%s) ORDER BY id",
            (run_ids,),
        ).fetchall()
    assert rows == [
        ("partial", 3, "1 of 2 feeds failed: zz_test_bad"),
        ("partial", 0, "1 of 2 feeds failed: zz_test_bad"),
    ]
