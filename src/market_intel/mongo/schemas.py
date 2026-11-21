"""Collections, their $jsonSchema validators and their indexes: the source of truth for
``mongo_setup``.

Validators use validationLevel "strict" and validationAction "error": every insert and update
is checked, and a bad document is rejected. They mirror the Pydantic models in models.py
(required fields, types, allowed values; a unit test compares the required lists).
Validators check shape, not meaning: the leakage guard lives in the PredictionAudit model.

To change a collection: edit the model and the validator here, then run mongo_setup (it
updates validators with collMod and creates missing indexes; it never drops anything).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from market_intel.mongo.models import LLMExtraction, NewsItem, PredictionAudit

VALIDATION_LEVEL = "strict"
VALIDATION_ACTION = "error"

# --- small builders for $jsonSchema ----------------------------------------------------------


def _t(*types: str, **extra: Any) -> dict[str, Any]:
    return {"bsonType": types[0] if len(types) == 1 else list(types), **extra}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if "enum" in schema:
        return {**schema, "enum": [*schema["enum"], None]}
    types = schema["bsonType"]
    types = [types] if isinstance(types, str) else list(types)
    return {**schema, "bsonType": [*types, "null"]}


def _enum(*values: str) -> dict[str, Any]:
    return {"enum": list(values)}


def _object(required: list[str], properties: dict[str, Any], *, top: bool = False):
    props = {"_id": _t("objectId"), **properties} if top else properties
    return {
        "bsonType": "object",
        "required": required,
        "properties": props,
        "additionalProperties": False,  # like extra="forbid" in the models
    }


STR = _t("string")
DATE = _t("date")
BOOL = _t("bool")
OID = _t("objectId")
NUM = _t("double", "int", "long", "decimal")
INT = _t("int", "long")
NON_NEG_INT = _t("int", "long", minimum=0)
NON_NEG_NUM = _t("double", "int", "long", "decimal", minimum=0)
STR_LIST = _t("array", items=STR)
VERSION = _t("int", enum=[1])
UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SIGNAL = _enum("up", "down", "hold")

# --- validators ------------------------------------------------------------------------------

NEWS_ITEMS_SCHEMA = _object(
    [
        "schema_version",
        "source",
        "feed_url",
        "url",
        "url_hash",
        "title",
        "published_at",
        "fetched_at",
        "available_at",
        "published_at_source",
        "kind",
        "language",
        "symbols",
    ],
    {
        "schema_version": VERSION,
        "source": STR,
        "feed_url": STR,
        "url": STR,
        "url_hash": _t("string", pattern="^[0-9a-f]{64}$"),
        "title": STR,
        "summary": _nullable(STR),
        "published_at": DATE,
        "fetched_at": DATE,
        "available_at": DATE,
        "published_at_source": _enum("feed", "fetched", "clamped"),
        "kind": _enum("news", "exchange", "company_news"),
        "publisher": _nullable(STR),
        "feed_name": _nullable(STR),
        "match_confidence": _nullable(_enum("high", "low")),
        "exchange_meta": _nullable(
            _object(
                ["exchange"],
                {
                    "exchange": STR,
                    "company_raw": _nullable(STR),
                    "symbol_raw": _nullable(STR),
                    "subject": _nullable(STR),
                    "attachment_url": _nullable(STR),
                },
            )
        ),
        "language": STR,
        "symbols": STR_LIST,
    },
    top=True,
)

EVENTS_SCHEMA = _object(
    [
        "war_conflict",
        "policy_change",
        "rbi_action",
        "budget_announcement",
        "order_win",
        "results_surprise",
        "promoter_activity",
        "sentiment",
    ],
    {
        "war_conflict": BOOL,
        "policy_change": _object(
            ["present"],
            {
                "present": BOOL,
                "direction": _nullable(_enum("positive", "negative", "neutral")),
            },
        ),
        "rbi_action": _enum("hike", "cut", "hold", "none"),
        "budget_announcement": BOOL,
        "order_win": _object(
            ["present"],
            {
                "present": BOOL,
                "amount_inr": _nullable(NUM),
                "sign": _nullable(_enum("positive", "negative")),
            },
        ),
        "results_surprise": _enum("beat", "miss", "inline", "none"),
        "promoter_activity": _enum("buying", "selling", "none"),
        "sentiment": _t("double", "int", "long", "decimal", minimum=-1, maximum=1),
    },
)

LLM_EXTRACTIONS_SCHEMA = _object(
    [
        "schema_version",
        "news_item_id",
        "published_at",
        "extracted_at",
        "provider",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cost_inr",
        "companies",
        "events",
    ],
    {
        "schema_version": VERSION,
        "news_item_id": OID,
        "published_at": DATE,
        "extracted_at": DATE,
        "provider": STR,
        "model": STR,
        "prompt_version": STR,
        "input_tokens": NON_NEG_INT,
        "output_tokens": NON_NEG_INT,
        "cost_inr": NON_NEG_NUM,
        "companies": STR_LIST,
        "events": EVENTS_SCHEMA,
    },
    top=True,
)

OUTCOME_SCHEMA = _object(
    ["filled_at", "actual_return_pct", "actual_direction", "correct"],
    {
        "filled_at": DATE,
        "actual_return_pct": NUM,
        "actual_direction": SIGNAL,
        "error": _nullable(NUM),
        "correct": BOOL,
    },
)

PREDICTION_AUDIT_SCHEMA = _object(
    [
        "schema_version",
        "prediction_id",
        "created_at",
        "instrument",
        "model",
        "horizon",
        "data_source",
        "features",
        "feature_cutoff",
        "shap_top",
        "news_events_used",
        "llm_calls",
        "signal",
        "confidence",
        "outcome_status",
    ],
    {
        "schema_version": VERSION,
        "prediction_id": _t("string", pattern=UUID_PATTERN),
        "created_at": DATE,
        "instrument": _object(
            ["symbol"],
            {"symbol": STR, "isin": _nullable(STR), "instrument_id": _nullable(INT)},
        ),
        "model": _object(["name", "version"], {"name": STR, "version": STR}),
        "horizon": _enum("15m", "1h", "eod", "1M", "3M", "6M"),
        "data_source": _enum("upstox", "yfinance"),
        "features": _t("object"),
        "feature_cutoff": DATE,
        "shap_top": _t(
            "array",
            items=_object(
                ["feature", "contribution", "reason_text"],
                {
                    "feature": STR,
                    "value": _t("double", "int", "long", "decimal", "string", "bool", "null"),
                    "contribution": NUM,
                    "reason_text": STR,
                },
            ),
        ),
        "news_events_used": _t(
            "array",
            items=_object(
                ["news_item_id", "url", "published_at"],
                {"news_item_id": OID, "url": STR, "published_at": DATE},
            ),
        ),
        "llm_calls": _t(
            "array",
            items=_object(
                ["provider", "model", "prompt_version", "cost_inr"],
                {"provider": STR, "model": STR, "prompt_version": STR, "cost_inr": NON_NEG_NUM},
            ),
        ),
        "signal": SIGNAL,
        "predicted_return_pct": _nullable(NUM),
        "confidence": _t("double", "int", "long", "decimal", minimum=0, maximum=1),
        "outcome_status": _enum("pending", "filled"),
        "outcome": _nullable(OUTCOME_SCHEMA),
    },
    top=True,
)

# --- collections and indexes -----------------------------------------------------------------


@dataclass(frozen=True)
class IndexSpec:
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    model: type[BaseModel] | None
    schema: dict[str, Any] | None  # None: no validator
    indexes: tuple[IndexSpec, ...] = field(default=())

    @property
    def validator(self) -> dict[str, Any] | None:
        return None if self.schema is None else {"$jsonSchema": self.schema}


ASC, DESC = 1, -1

NEWS_ITEMS = CollectionSpec(
    "news_items",
    NewsItem,
    NEWS_ITEMS_SCHEMA,
    (
        IndexSpec("uq_url_hash", (("url_hash", ASC),), unique=True),
        IndexSpec("ix_published_at", (("published_at", DESC),)),
        IndexSpec("ix_symbols_published_at", (("symbols", ASC), ("published_at", DESC))),
        IndexSpec("ix_available_at", (("available_at", DESC),)),
        IndexSpec("ix_kind_available_at", (("kind", ASC), ("available_at", DESC))),
    ),
)
LLM_EXTRACTIONS = CollectionSpec(
    "llm_extractions",
    LLMExtraction,
    LLM_EXTRACTIONS_SCHEMA,
    (
        IndexSpec(
            "uq_news_item_id_prompt_version",
            (("news_item_id", ASC), ("prompt_version", ASC)),
            unique=True,
        ),
        IndexSpec("ix_published_at", (("published_at", DESC),)),
    ),
)
PREDICTION_AUDIT = CollectionSpec(
    "prediction_audit",
    PredictionAudit,
    PREDICTION_AUDIT_SCHEMA,
    (
        IndexSpec("uq_prediction_id", (("prediction_id", ASC),), unique=True),
        IndexSpec("ix_symbol_created_at", (("instrument.symbol", ASC), ("created_at", DESC))),
        IndexSpec(
            "ix_model_created_at",
            (("model.name", ASC), ("model.version", ASC), ("created_at", DESC)),
        ),
        IndexSpec("ix_outcome_status_created_at", (("outcome_status", ASC), ("created_at", ASC))),
    ),
)
HEALTHCHECK = CollectionSpec("_healthcheck", None, None)  # used by db_check's write test

COLLECTIONS: tuple[CollectionSpec, ...] = (
    NEWS_ITEMS,
    LLM_EXTRACTIONS,
    PREDICTION_AUDIT,
    HEALTHCHECK,
)
