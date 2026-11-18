"""The MongoDB app login (MONGODB_URI, role mi_app_rw) reads and writes documents, the
validators reject bad documents, and collections, indexes and validators can't be changed.

Runs in CI after mongo_setup (RUN_INTEGRATION=1). Only fake "DEMO" data; cleans up after itself.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import OperationFailure, WriteError

from market_intel.config import get_settings
from market_intel.db import mongo
from market_intel.mongo import audit
from market_intel.mongo.models import NewsItem, Outcome, PredictionAudit

pytestmark = pytest.mark.integration

UNAUTHORIZED = 13
VALIDATION_FAILED = 121
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def app_db() -> Iterator[Database]:
    db_settings = get_settings().require("database")
    with mongo.client(db_settings) as client:
        yield client[db_settings.mongodb_db]


def demo_news() -> NewsItem:
    return NewsItem(
        source="demo",
        feed_url="https://example.com/feed",
        url=f"https://example.com/demo/{ObjectId()}",
        title="DEMO integration test",
        published_at=NOW - timedelta(hours=1),
        symbols=["DEMO"],
    )


def demo_audit() -> PredictionAudit:
    return PredictionAudit(
        created_at=NOW,
        instrument={"symbol": "DEMO"},
        model={"name": "demo", "version": "0"},
        horizon="eod",
        data_source="yfinance",
        features={"demo_feature": 1.0},
        feature_cutoff=NOW - timedelta(minutes=5),
        signal="hold",
        confidence=0.5,
    )


def test_app_can_insert_and_find(app_db):
    item = demo_news()
    result = app_db.news_items.insert_one(item.to_document())
    try:
        found = app_db.news_items.find_one({"_id": result.inserted_id})
        assert found["url_hash"] == item.url_hash
        assert found["published_at"] == item.published_at  # stored and read back as UTC
    finally:
        app_db.news_items.delete_one({"_id": result.inserted_id})


def test_validator_rejects_news_without_published_at(app_db):
    doc = demo_news().to_document()
    del doc["published_at"]
    with pytest.raises(WriteError) as exc:
        app_db.news_items.insert_one(doc)
    assert exc.value.code == VALIDATION_FAILED


def test_validator_rejects_unknown_outcome_status(app_db):
    doc = {**demo_audit().to_document(), "outcome_status": "maybe"}
    with pytest.raises(WriteError) as exc:
        app_db.prediction_audit.insert_one(doc)
    assert exc.value.code == VALIDATION_FAILED


def test_insert_into_missing_collection_is_refused(app_db):
    with pytest.raises(OperationFailure) as exc:
        app_db.zz_missing_probe.insert_one({"demo": True})
    assert exc.value.code == UNAUTHORIZED


@pytest.mark.parametrize(
    "action",
    [
        pytest.param(lambda db: db.drop_collection("news_items"), id="dropCollection"),
        pytest.param(lambda db: db.news_items.create_index("title"), id="createIndex"),
        pytest.param(
            lambda db: db.command("collMod", "news_items", validationAction="warn"), id="collMod"
        ),
        pytest.param(lambda db: db.create_collection("zz_probe"), id="createCollection"),
    ],
)
def test_app_cannot_change_collections(app_db, action):
    with pytest.raises(OperationFailure) as exc:
        action(app_db)
    assert exc.value.code == UNAUTHORIZED


def test_audit_outcome_once_then_refused(app_db, setup_db):
    prediction_id = audit.record_prediction(app_db, demo_audit())
    try:
        outcome = Outcome(
            filled_at=NOW + timedelta(hours=6),
            actual_return_pct=0.0,
            actual_direction="hold",
            correct=True,
        )
        audit.record_outcome(app_db, prediction_id, outcome)
        doc = app_db.prediction_audit.find_one({"prediction_id": prediction_id})
        assert doc["outcome_status"] == "filled" and doc["outcome"]["correct"] is True
        with pytest.raises(audit.AuditError):
            audit.record_outcome(app_db, prediction_id, outcome)
    finally:
        # Cleanup with the setup login: the app login has no remove right on audits.
        setup_db.prediction_audit.delete_one({"prediction_id": prediction_id})
