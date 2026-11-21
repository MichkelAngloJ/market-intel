"""1.2b as the app logins: migration 0005 (bse_code), the exchange-key migration on FAKE items,
company_news inserts with $addToSet, and the validator refusing an unknown kind.

Fake items use feed names starting with "zz_test_" and are deleted afterwards; Postgres changes
run in a transaction that is rolled back. Safe on a real local database. Runs in CI
compose-smoke (RUN_INTEGRATION=1).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import WriteError

from market_intel.config import get_settings
from market_intel.db import mongo, postgres
from market_intel.mongo.models import NewsItem, exchange_identity, item_key, url_hash
from market_intel.news import migrate_exchange_keys, store_company_items

pytestmark = pytest.mark.integration

VALIDATION_FAILED = 121
NOW = datetime.now(UTC).replace(microsecond=0)
ZZ = {"feed_name": {"$regex": "^zz_test_"}}


@pytest.fixture
def app_db() -> Iterator[Database]:
    db = get_settings().require("database")
    with mongo.client(db) as client:
        database = client[db.mongodb_db]
        database.news_items.delete_many(ZZ)
        yield database
        database.news_items.delete_many(ZZ)


@pytest.fixture
def pg() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


def test_0005_bse_code_as_the_app_login(pg):
    assert pg.execute("SELECT current_user").fetchone()[0] == "mi_app"
    ids = [
        pg.execute(
            "INSERT INTO instruments (segment, symbol, is_active, bse_code)"
            " VALUES ('EQ', %s, false, %s) RETURNING id",
            (f"ZZBSE{i}", None),
        ).fetchone()[0]
        for i in range(3)
    ]
    pg.execute("UPDATE instruments SET bse_code = 'ZZ0001' WHERE id = %s", (ids[0],))
    assert (
        pg.execute("SELECT bse_code FROM instruments WHERE id = %s", (ids[0],)).fetchone()[0]
        == "ZZ0001"
    )
    with pytest.raises(psycopg.errors.UniqueViolation), pg.transaction():
        pg.execute("UPDATE instruments SET bse_code = 'ZZ0001' WHERE id = %s", (ids[1],))
    # many rows without a code are fine (unique only where not null)
    assert (
        pg.execute(
            "SELECT count(*) FROM instruments WHERE id = ANY(%s) AND bse_code IS NULL", (ids,)
        ).fetchone()[0]
        == 2
    )


def raw_exchange(title: str, url: str) -> dict:
    """An exchange item as 1.2 stored it: keyed by the plain URL hash."""
    doc = NewsItem(
        source="rss:zz_test_nse", feed_url="https://zz.example.invalid/rss", url=url,
        title=title, published_at=NOW - timedelta(minutes=5), fetched_at=NOW,
        kind="exchange", feed_name="zz_test_nse", url_hash=url_hash(url),
        exchange_meta={"exchange": "NSE", "company_raw": title},
    ).to_document()  # fmt: skip
    return doc


def test_exchange_key_migration_on_fake_items(app_db):
    url = f"https://zz.example.invalid/{ObjectId()}.pdf"
    app_db.news_items.insert_one(raw_exchange("Zz Fake Alpha Limited", url))
    first = migrate_exchange_keys(app_db.news_items, only=ZZ)
    assert (first.updated, first.unchanged, first.conflicts) == (1, 0, [])
    stored = app_db.news_items.find_one({"feed_name": "zz_test_nse"})
    published = stored["published_at"]
    assert stored["url_hash"] == item_key(
        url, exchange_identity("Zz Fake Alpha Limited", published)
    )
    again = migrate_exchange_keys(app_db.news_items, only=ZZ)
    assert (again.updated, again.unchanged) == (0, 1)
    # the same PDF for a second filing is now a separate item
    second = raw_exchange("Zz Fake Alpha Limited (2)", url)
    second.pop("url_hash")
    doc = NewsItem(**{k: v for k, v in second.items() if k != "_id"}).to_document()
    app_db.news_items.insert_one(doc)
    assert app_db.news_items.count_documents({"feed_name": "zz_test_nse"}) == 2


def company_doc(symbol: str, link: str) -> dict:
    return NewsItem(
        source="google_news", feed_url="https://zz.example.invalid/rss/search?q=x",
        url=link, title="Zz Fake Alpha and Zz Fake Beta merge", published_at=NOW,
        fetched_at=NOW, kind="company_news", publisher="Zz Times", feed_name="zz_test_google",
        symbols=[symbol], match_confidence="high",
    ).to_document()  # fmt: skip


def test_company_news_insert_and_add_to_set(app_db):
    link = f"https://zz.example.invalid/rss/articles/{ObjectId()}"
    assert store_company_items(app_db.news_items, [company_doc("ZZA", link)], "ZZA") == (1, 0, 0)
    assert store_company_items(app_db.news_items, [company_doc("ZZB", link)], "ZZB") == (0, 1, 0)
    found = app_db.news_items.find_one({"feed_name": "zz_test_google"})
    assert found["symbols"] == ["ZZA", "ZZB"] and found["match_confidence"] == "high"
    assert app_db.news_items.count_documents({"feed_name": "zz_test_google"}) == 1


def test_validator_rejects_an_unknown_kind(app_db):
    doc = company_doc("ZZA", f"https://zz.example.invalid/{ObjectId()}")
    doc["kind"] = "blog"
    with pytest.raises(WriteError) as exc:
        app_db.news_items.insert_one(doc)
    assert exc.value.code == VALIDATION_FAILED
