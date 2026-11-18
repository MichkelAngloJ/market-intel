"""Mongo document models, their $jsonSchema validators and the append-only audit writer.
Fakes only: no database."""

import types
import typing
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from bson import ObjectId
from pydantic import BaseModel, ValidationError

from market_intel.mongo import audit
from market_intel.mongo.models import (
    LLMExtraction,
    NewsItem,
    Outcome,
    PredictionAudit,
    normalise_url,
    url_hash,
)
from market_intel.mongo.schemas import COLLECTIONS, VALIDATION_ACTION, VALIDATION_LEVEL

T0 = datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))


def news(**overrides):
    fields = dict(
        source="demo-feed",
        feed_url="https://example.com/feed",
        url="https://Example.com/markets/demo-story/?utm_source=x&b=2&a=1#top",
        title="DEMO story",
        published_at=T0 - timedelta(hours=1),
    )
    return NewsItem(**{**fields, **overrides})


def extraction(**overrides):
    fields = dict(
        news_item_id=str(ObjectId()),
        published_at=T0 - timedelta(hours=1),
        provider="demo",
        model="demo-model",
        prompt_version="v1",
        input_tokens=100,
        output_tokens=20,
        cost_inr=0.05,
        events=dict(
            war_conflict=False,
            policy_change=dict(present=False),
            rbi_action="none",
            budget_announcement=False,
            order_win=dict(present=True, amount_inr=1e7, sign="positive"),
            results_surprise="none",
            promoter_activity="none",
            sentiment=0.4,
        ),
    )
    return LLMExtraction(**{**fields, **overrides})


def prediction(**overrides):
    fields = dict(
        created_at=T0,
        instrument=dict(symbol="DEMO"),
        model=dict(name="demo-model", version="0.0.1"),
        horizon="eod",
        data_source="yfinance",
        features={"rsi_14": 55.2},
        feature_cutoff=T0 - timedelta(minutes=5),
        news_events_used=[
            dict(news_item_id=ObjectId(), url="https://example.com/a", published_at=T0)
        ],
        signal="up",
        confidence=0.6,
    )
    return PredictionAudit(**{**fields, **overrides})


# --- news_items --------------------------------------------------------------------------------


def test_news_item_computes_url_hash_from_normalised_url():
    item = news()
    assert normalise_url(item.url) == "https://example.com/markets/demo-story?a=1&b=2"
    assert item.url_hash == url_hash("https://example.com/markets/demo-story?b=2&a=1")
    assert len(item.url_hash) == 64


def test_news_item_requires_published_at():
    with pytest.raises(ValidationError, match="published_at"):
        news(published_at=None)


def test_news_item_rejects_wrong_url_hash():
    with pytest.raises(ValidationError, match="url_hash"):
        news(url_hash="0" * 64)


def test_datetimes_must_be_aware_and_are_stored_in_utc():
    with pytest.raises(ValidationError):
        news(published_at=datetime(2026, 11, 18, 9, 0))  # naive: refused
    item = news(published_at=datetime(2026, 11, 18, 15, 0, tzinfo=IST))
    assert item.published_at == datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
    assert item.published_at.tzinfo is UTC


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError, match="extra"):
        news(headline="typo")


def test_every_document_has_schema_version_1():
    assert news().to_document()["schema_version"] == 1
    assert extraction().to_document()["schema_version"] == 1
    assert prediction().to_document()["schema_version"] == 1


# --- llm_extractions ---------------------------------------------------------------------------


def test_extraction_converts_news_item_id_and_checks_ranges():
    assert isinstance(extraction().news_item_id, ObjectId)
    with pytest.raises(ValidationError, match="sentiment"):
        extraction(events={**extraction().events.model_dump(), "sentiment": 1.5})
    with pytest.raises(ValidationError, match="rbi_action"):
        extraction(events={**extraction().events.model_dump(), "rbi_action": "raise"})


# --- prediction_audit and the leakage guard ----------------------------------------------------


def test_prediction_audit_valid_and_pending():
    audit_doc = prediction()
    assert audit_doc.outcome_status == "pending" and audit_doc.outcome is None
    assert str(uuid.UUID(audit_doc.prediction_id)) == audit_doc.prediction_id


def test_leakage_guard_refuses_news_published_after_prediction():
    late = [
        dict(
            news_item_id=ObjectId(),
            url="https://example.com/late",
            published_at=T0 + timedelta(seconds=1),
        )
    ]
    with pytest.raises(ValidationError, match="leakage: news .* is after created_at"):
        prediction(news_events_used=late)


def test_leakage_guard_refuses_feature_cutoff_after_prediction():
    with pytest.raises(ValidationError, match="leakage: feature_cutoff"):
        prediction(feature_cutoff=T0 + timedelta(minutes=1))


def test_leakage_guard_compares_in_utc():
    # 15:00 IST is 09:30 UTC = created_at: equal is allowed.
    same_moment = datetime(2026, 11, 18, 15, 0, tzinfo=IST)
    assert prediction(feature_cutoff=same_moment).feature_cutoff == T0


def test_outcome_and_status_must_agree():
    with pytest.raises(ValidationError, match="outcome must be set"):
        prediction(outcome_status="filled")


def test_prediction_id_must_be_a_uuid():
    with pytest.raises(ValidationError):
        prediction(prediction_id="not-a-uuid")


# --- validators mirror the models -------------------------------------------------------------


def _allows_none(annotation) -> bool:
    return type(None) in typing.get_args(annotation)


def _nested_model(annotation):
    """The BaseModel inside ``annotation`` (directly, in a list, or Optional), plus whether
    it sits in a list."""
    origin = typing.get_origin(annotation)
    if origin is list:
        inner = _nested_model(typing.get_args(annotation)[0])
        return (inner[0], True) if inner[0] else (None, False)
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            if arg is not type(None):
                return _nested_model(arg)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    return None, False


def _compare(model: type[BaseModel], schema: dict, path: str, top: bool = False) -> None:
    fields = model.model_fields
    expected_props = set(fields) | ({"_id"} if top else set())
    assert set(schema["properties"]) == expected_props, f"{path}: properties differ"
    required = {name for name, f in fields.items() if not _allows_none(f.annotation)}
    assert set(schema["required"]) == required, f"{path}: required differs"
    assert len(schema["required"]) == len(set(schema["required"])), f"{path}: duplicates"
    for name, f in fields.items():
        nested, in_list = _nested_model(f.annotation)
        if nested:
            sub = schema["properties"][name]
            _compare(nested, sub["items"] if in_list else sub, f"{path}.{name}")


@pytest.mark.parametrize("spec", [c for c in COLLECTIONS if c.model], ids=lambda c: c.name)
def test_validator_matches_model(spec):
    _compare(spec.model, spec.schema, spec.name, top=True)


def test_validator_settings_and_index_names():
    assert (VALIDATION_LEVEL, VALIDATION_ACTION) == ("strict", "error")
    for spec in COLLECTIONS:
        names = [i.name for i in spec.indexes]
        assert len(names) == len(set(names)), spec.name
        assert "_id_" not in names
    assert [c.name for c in COLLECTIONS] == [
        "news_items",
        "llm_extractions",
        "prediction_audit",
        "_healthcheck",
    ]


# --- append-only audit writer -----------------------------------------------------------------


class FakeAuditCollection:
    """Applies the update_one filter the way MongoDB would, for these simple filters."""

    def __init__(self):
        self.docs: list[dict] = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def update_one(self, flt, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in flt.items()):
                doc.update(update["$set"])
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)


@pytest.fixture
def fake_db():
    coll = FakeAuditCollection()
    return {audit.COLLECTION: coll}


def outcome():
    return Outcome(
        filled_at=T0 + timedelta(hours=6),
        actual_return_pct=0.8,
        actual_direction="up",
        correct=True,
    )


def test_record_prediction_inserts_pending(fake_db):
    pid = audit.record_prediction(fake_db, prediction())
    (doc,) = fake_db[audit.COLLECTION].docs
    assert doc["prediction_id"] == pid
    assert doc["outcome_status"] == "pending" and doc["outcome"] is None


def test_record_outcome_once_then_refused(fake_db):
    pid = audit.record_prediction(fake_db, prediction())
    audit.record_outcome(fake_db, pid, outcome())
    (doc,) = fake_db[audit.COLLECTION].docs
    assert doc["outcome_status"] == "filled" and doc["outcome"]["correct"] is True
    with pytest.raises(audit.AuditError, match="already filled or unknown"):
        audit.record_outcome(fake_db, pid, outcome())


def test_record_outcome_unknown_id_refused(fake_db):
    with pytest.raises(audit.AuditError):
        audit.record_outcome(fake_db, str(uuid.uuid4()), outcome())


def test_record_prediction_refuses_a_filled_record(fake_db):
    filled = prediction(outcome_status="filled", outcome=outcome())
    with pytest.raises(audit.AuditError, match="pending"):
        audit.record_prediction(fake_db, filled)
    assert fake_db[audit.COLLECTION].docs == []


def test_audit_module_offers_no_other_writes():
    public = {n for n in dir(audit) if not n.startswith("_") and callable(getattr(audit, n))}
    writers = public - {"AuditError", "Database", "Outcome", "PredictionAudit"}
    assert writers == {"record_prediction", "record_outcome"}
