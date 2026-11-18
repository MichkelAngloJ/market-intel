"""Delete rights of the MongoDB app login: refused on prediction_audit (append-only audits),
allowed on news_items. Runs in CI (RUN_INTEGRATION=1); fake "DEMO" data only."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import OperationFailure

from market_intel.config import get_settings
from market_intel.db import mongo
from market_intel.mongo import audit
from market_intel.mongo.access import collection
from market_intel.mongo.models import NewsItem, PredictionAudit

pytestmark = pytest.mark.integration

UNAUTHORIZED = 13
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def app_db() -> Iterator[Database]:
    db_settings = get_settings().require("database")
    with mongo.client(db_settings) as client:
        yield client[db_settings.mongodb_db]


def test_app_cannot_delete_audits(app_db, setup_db):
    prediction_id = audit.record_prediction(
        app_db,
        PredictionAudit(
            created_at=NOW,
            instrument={"symbol": "DEMO"},
            model={"name": "demo", "version": "0"},
            horizon="eod",
            data_source="yfinance",
            features={},
            feature_cutoff=NOW,
            signal="hold",
            confidence=0.5,
        ),
    )
    try:
        audits = collection(app_db, "prediction_audit")
        for delete in (audits.delete_one, audits.delete_many):
            with pytest.raises(OperationFailure) as exc:
                delete({"prediction_id": prediction_id})
            assert exc.value.code == UNAUTHORIZED
        assert audits.count_documents({"prediction_id": prediction_id}) == 1
    finally:
        setup_db.prediction_audit.delete_one({"prediction_id": prediction_id})


def test_app_can_delete_news_items(app_db):
    news = collection(app_db, "news_items")
    item = NewsItem(
        source="demo",
        feed_url="https://example.com/feed",
        url=f"https://example.com/demo/{ObjectId()}",
        title="DEMO delete test",
        published_at=NOW - timedelta(hours=1),
    )
    inserted = news.insert_one(item.to_document()).inserted_id
    assert news.delete_one({"_id": inserted}).deleted_count == 1
    assert news.delete_many({"_id": "zz-no-such-id"}).deleted_count == 0  # allowed, no match
