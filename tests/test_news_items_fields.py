"""news_items fields from 1.2 (timing, kind, publisher, feed, exchange_meta), the validator and
indexes that mirror them, mongo_setup's update of an older validator, and the "partial" run
status of the ingestion helper."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from market_intel import mongo_setup
from market_intel.config import load_settings
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import configure_logging
from market_intel.mongo.models import NewsItem
from market_intel.mongo.schemas import NEWS_ITEMS, NEWS_ITEMS_SCHEMA

T0 = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
FAKE_PW = "fake-pass-111"


def news(**overrides):
    fields = dict(
        source="rss:fake",
        feed_url="https://feed.example.invalid/rss",
        url="https://news.example.invalid/a",
        title="Fake headline",
        published_at=T0,
        fetched_at=T0 + timedelta(minutes=2),
    )
    return NewsItem(**{**fields, **overrides})


def test_available_at_is_filled_as_the_later_time():
    assert news().available_at == T0 + timedelta(minutes=2)
    assert news(fetched_at=T0 - timedelta(minutes=1)).available_at == T0


def test_a_wrong_available_at_is_refused():
    with pytest.raises(ValidationError, match="available_at must be max"):
        news(available_at=T0)


def test_defaults_for_older_callers():
    item = news()
    assert (item.kind, item.published_at_source, item.publisher, item.feed_name) == (
        "news",
        "feed",
        None,
        None,
    )
    assert item.exchange_meta is None


def test_exchange_meta_is_strict():
    item = news(kind="exchange", exchange_meta={"exchange": "NSE", "company_raw": "Fake Ltd"})
    assert item.exchange_meta.company_raw == "Fake Ltd"
    with pytest.raises(ValidationError, match="extra"):
        news(exchange_meta={"exchange": "NSE", "price": 1})
    with pytest.raises(ValidationError):
        news(kind="blog")
    with pytest.raises(ValidationError):
        news(published_at_source="guessed")


def test_validator_requires_the_new_fields_and_lists_the_enums():
    required = set(NEWS_ITEMS_SCHEMA["required"])
    assert {"available_at", "published_at_source", "kind"} <= required
    props = NEWS_ITEMS_SCHEMA["properties"]
    assert props["kind"]["enum"] == ["news", "exchange", "company_news"]
    assert props["published_at_source"]["enum"] == ["feed", "fetched", "clamped"]
    meta = props["exchange_meta"]
    assert meta["additionalProperties"] is False and meta["required"] == ["exchange"]
    assert set(meta["bsonType"]) == {"object", "null"}


def test_new_indexes():
    names = {i.name: i.keys for i in NEWS_ITEMS.indexes}
    assert names["ix_available_at"] == (("available_at", -1),)
    assert names["ix_kind_available_at"] == (("kind", 1), ("available_at", -1))


def test_mongo_setup_updates_an_older_news_validator_then_has_nothing_to_change(capsys):
    from test_mongo_setup import FakeDb

    db = FakeDb()
    old_schema = {**NEWS_ITEMS_SCHEMA, "required": ["url_hash"]}
    db.create_collection("news_items", validator={"$jsonSchema": old_schema})
    for spec in NEWS_ITEMS.indexes[:3]:  # the 0.6 indexes exist already
        db.colls["news_items"].create_index(list(spec.keys), name=spec.name, unique=spec.unique)
    db.calls.clear()
    assert mongo_setup.run(db, "db.invalid") == 0
    out = capsys.readouterr().out
    assert "collMod news_items" in db.calls
    assert db.options["news_items"]["validator"] == NEWS_ITEMS.validator
    assert {"ix_available_at", "ix_kind_available_at"} <= set(db.colls["news_items"].indexes)
    assert "updated  news_items: validator (collMod)" in out
    db.calls.clear()
    assert mongo_setup.run(db, "db.invalid") == 0
    assert "nothing to change" in capsys.readouterr().out and db.calls == []


# --- partial runs ------------------------------------------------------------------------


class Conn:
    def __init__(self):
        self.updates: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("INSERT"):
            return type("R", (), {"fetchone": lambda self: (7,)})()
        self.updates.append((sql, params))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in {
        "APP_PROFILE": "full",
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
    }.items():
        monkeypatch.setenv(key, value)
    s = load_settings()
    configure_logging(s, "ingestion_test", console_stream=io.StringIO())
    return s


def test_mark_partial_records_status_rows_and_the_masked_reason(settings):
    conn = Conn()
    fake_link = "https://" + "someone" + ":" + "pw-not-real-2" + "@feed.invalid/x"
    with ingestion_run(settings, "news_fetch", connect=lambda db: conn) as run:
        run.add_rows(4)
        run.mark_partial(f"1 of 3 feeds failed: {fake_link}")
    [(sql, params)] = conn.updates
    assert "status = 'partial'" in sql
    assert params[0] == 4 and params[2] == 7
    assert "someone" not in params[1] and "pw-not-real-2" not in params[1]


def test_a_partial_run_needs_a_reason(settings):
    conn = Conn()
    with pytest.raises(ValueError, match="needs a reason"):
        with ingestion_run(settings, "news_fetch", connect=lambda db: conn) as run:
            run.mark_partial("")
    assert "'failed'" in conn.updates[0][0]


def test_without_mark_partial_the_run_is_a_success(settings):
    conn = Conn()
    with ingestion_run(settings, "news_fetch", connect=lambda db: conn):
        pass
    assert "status = 'success'" in conn.updates[0][0]
