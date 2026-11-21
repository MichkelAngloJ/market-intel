"""Part A (1.2b): exchange items are keyed by their own identity (guid, or title + time), news
items by their URL; the migrate-exchange-keys command is idempotent and reports conflicts."""

from datetime import UTC, datetime, timedelta

from bson import ObjectId
from news_fakes import T0, item, rss
from pymongo.errors import DuplicateKeyError

from market_intel.mongo.models import NewsItem, exchange_identity, item_key, url_hash
from market_intel.news import migrate_exchange_keys
from market_intel.sources.rss import Feed, parse

NSE = Feed("fake_nse", "https://nse.example.invalid/feed", "exchange", "NSE")
NEWS = Feed("fake_news", "https://news.example.invalid/feed", "news", "Fake Times")
PDF = "https://nse.example.invalid/shared.pdf"


def test_news_items_keep_the_plain_url_hash():
    [record] = parse(NEWS, rss(item()), T0).records
    assert record.to_document().url_hash == url_hash("https://news.example.invalid/a1")


def test_exchange_items_sharing_one_link_are_separate_events():
    body = rss(
        item("Fake Alpha Limited", PDF, "Acquirer one", pub="22-Nov-2026 10:00"),
        item("Fake Alpha Limited", PDF, "Acquirer two", pub="22-Nov-2026 10:05"),
        item("Fake Alpha Limited", PDF, "Same item again", pub="22-Nov-2026 10:05"),
    )
    records = parse(NSE, body, T0).records
    assert len(records) == 2  # the third repeats the second's title and time
    keys = {r.to_document().url_hash for r in records}
    assert len(keys) == 2 and url_hash(PDF) not in keys


def test_the_key_is_link_plus_title_and_time_without_a_guid():
    [record] = parse(
        NSE, rss(item("Fake  Alpha Limited", PDF, "x", pub="22-Nov-2026 10:00")), T0
    ).records
    published = datetime(2026, 11, 22, 4, 30, tzinfo=UTC)
    expected = item_key(PDF, f"fake alpha limited|{published.isoformat()}")
    assert record.to_document().url_hash == expected
    assert (
        exchange_identity("Fake  Alpha Limited", published)
        == f"fake alpha limited|{published.isoformat()}"
    )


def test_a_feed_guid_is_the_identity_when_present():
    body = rss(item("Fake Alpha Limited", PDF, "x", extra="<guid isPermaLink='false'>g-77</guid>"))
    [record] = parse(NSE, body, T0).records
    assert record.to_document().url_hash == item_key(PDF, "g-77")


def test_an_undated_exchange_item_keeps_one_key_across_fetches():
    body = rss(item("Fake Alpha Limited", PDF, "x", pub=None))
    first = parse(NSE, body, T0).records[0].to_document().url_hash
    later = parse(NSE, body, T0 + timedelta(hours=2)).records[0].to_document().url_hash
    assert first == later  # the fetch time is not part of the identity


def test_the_model_computes_the_exchange_key_when_none_is_given():
    doc = NewsItem(
        source="rss:x", feed_url="https://f.invalid", url=PDF, title="Fake Alpha Limited",
        published_at=T0, fetched_at=T0, kind="exchange",
    )  # fmt: skip
    assert doc.url_hash == item_key(PDF, exchange_identity("Fake Alpha Limited", T0))


# --- migrate-exchange-keys ---------------------------------------------------------------


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt, projection=None):
        return [dict(d) for d in self.docs if d.get("kind") == flt["kind"]]

    def find_one(self, flt, projection=None):
        return next((d for d in self.docs if d["url_hash"] == flt["url_hash"]), None)

    def update_one(self, flt, update):
        new = update["$set"]["url_hash"]
        if any(d["url_hash"] == new for d in self.docs):
            raise DuplicateKeyError("dup")
        for d in self.docs:
            if d["_id"] == flt["_id"] and d["url_hash"] == flt["url_hash"]:
                d["url_hash"] = new
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()


def stored(title, url=PDF, when=T0, kind="exchange", key=None):
    return {
        "_id": ObjectId(),
        "kind": kind,
        "url": url,
        "url_hash": key or url_hash(url),
        "title": title,
        "published_at": when,
        "published_at_source": "feed",
    }


def test_migration_recomputes_keys_once_and_is_idempotent():
    news = stored("Fake headline", url="https://news.invalid/a", kind="news")
    docs = [stored("Fake Alpha Limited"), news]
    coll = FakeCollection(docs)
    dry = migrate_exchange_keys(coll, dry_run=True)
    assert (dry.updated, dry.unchanged, dry.conflicts) == (1, 0, [])
    assert docs[0]["url_hash"] == url_hash(PDF)  # dry run changed nothing
    real = migrate_exchange_keys(coll)
    assert real.updated == 1 and docs[0]["url_hash"] == item_key(
        PDF, exchange_identity("Fake Alpha Limited", T0)
    )
    again = migrate_exchange_keys(coll)
    assert (again.updated, again.unchanged, again.conflicts) == (0, 1, [])
    assert news["url_hash"] == url_hash("https://news.invalid/a")  # news untouched
    assert real.lines()[0] == "exchange item keys: updated 1, unchanged 0, conflicts 0"


def test_a_conflict_changes_nothing_for_that_item_and_is_reported():
    target = item_key(PDF, exchange_identity("Fake Alpha Limited", T0))
    # an item that is not migrated (a news item) already holds the new key
    holder = stored("Fake Alpha Limited", url="https://other.invalid/x", kind="news", key=target)
    old = stored("Fake Alpha Limited")
    coll = FakeCollection([holder, old])
    summary = migrate_exchange_keys(coll)
    assert summary.conflicts == [str(old["_id"])]
    assert old["url_hash"] == url_hash(PDF)
    assert "conflict (left unchanged)" in summary.lines()[1]
