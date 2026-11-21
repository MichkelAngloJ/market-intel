"""Pydantic v2 models for the MongoDB documents. Every document has ``schema_version`` 1, and
every datetime must be timezone-aware; it is stored in UTC.

The database checks the same shape with $jsonSchema validators (schemas.py); a unit test
keeps the two in step.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bson import ObjectId
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

SCHEMA_VERSION = 1


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _to_object_id(value: Any) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    raise ValueError("must be an ObjectId or its 24-character hex string")


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(value))  # raises ValueError if it isn't a UUID


# Naive datetimes are rejected (no guessing the time zone); aware ones are converted to UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
ObjectIdField = Annotated[ObjectId, BeforeValidator(_to_object_id)]
UuidStr = Annotated[str, AfterValidator(_canonical_uuid)]
_UTC: TypeAdapter[datetime] = TypeAdapter(UtcDatetime)

Signal = Literal["up", "down", "hold"]
Horizon = Literal["15m", "1h", "eod", "1M", "3M", "6M"]
DataSource = Literal["upstox", "yfinance"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Strict(BaseModel):
    """Unknown fields are errors, so a typo can't slip into the database."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)


class Document(_Strict):
    """A top-level MongoDB document."""

    schema_version: Literal[1] = SCHEMA_VERSION

    def to_document(self) -> dict[str, Any]:
        """The dict to insert. Datetimes stay aware (PyMongo stores them as UTC)."""
        return self.model_dump()


# --- news_items ------------------------------------------------------------------------------

# Tracking parameters that don't change which article a URL points to.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def normalise_url(url: str) -> str:
    """One spelling per article: lower-case scheme and host, no default port, no fragment,
    no tracking parameters, sorted query, no trailing slash (except the root)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    """sha256 (hex) of the normalised URL: the item key of a news or company_news item."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()


def exchange_identity(title: str, published_at: datetime, published_at_source: str = "feed") -> str:
    """The identity of an exchange item whose feed gives no guid: the normalised title, plus
    "|" and the publish time (UTC ISO) when that time came from the feed itself (a fetch time
    would change on every fetch and turn one filing into many)."""
    text = " ".join(title.split()).casefold()
    if published_at_source != "feed":
        return text
    return f"{text}|{published_at.astimezone(UTC).isoformat()}"


def item_key(url: str, identity: str | None = None) -> str:
    """The item key stored as ``url_hash``. News and company news: sha256 of the normalised
    URL. Exchange items: sha256 of the normalised URL + "#" + the item's identity (the feed's
    guid, or :func:`exchange_identity`), so filings that share one PDF stay separate events."""
    if identity is None:
        return url_hash(url)
    return hashlib.sha256(f"{normalise_url(url)}#{identity}".encode()).hexdigest()


_HEX64 = re.compile(r"^[0-9a-f]{64}$")

NewsKind = Literal["news", "exchange", "company_news"]
# company_news only: high unless the company name is one word or isn't in the title/summary
MatchConfidence = Literal["high", "low"]
# Where published_at came from: the feed's own date, the fetch time (the feed gave none), or
# the fetch time because the feed's date was more than 5 minutes in the future.
PublishedAtSource = Literal["feed", "fetched", "clamped"]


class ExchangeMeta(_Strict):
    """What an exchange announcement says about itself, as given by the feed (raw text)."""

    exchange: str  # NSE or BSE
    company_raw: str | None = None
    symbol_raw: str | None = None  # the exchange's own code: an NSE symbol or a BSE scrip code
    subject: str | None = None
    attachment_url: str | None = None  # stored only; never downloaded


class NewsItem(Document):
    source: str
    feed_url: str
    url: str
    url_hash: str  # filled in from ``url`` if not given
    title: str
    summary: str | None = None
    published_at: UtcDatetime  # when the source says it became public
    fetched_at: UtcDatetime = Field(default_factory=utc_now)  # when we first fetched it
    # When OUR system could know it: max(published_at, fetched_at). Phase 2 features use this.
    # Filled in if not given; a given value must equal that maximum.
    available_at: UtcDatetime
    published_at_source: PublishedAtSource = "feed"
    kind: NewsKind = "news"
    publisher: str | None = None
    feed_name: str | None = None
    match_confidence: MatchConfidence | None = None
    exchange_meta: ExchangeMeta | None = None
    language: str = "en"
    symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fill_url_hash(cls, data: Any) -> Any:
        # url_hash is the ITEM KEY (see item_key). Exchange items with a feed guid arrive with
        # their key computed; without one it is computed here from the title and time.
        if not (isinstance(data, dict) and isinstance(data.get("url"), str)):
            return data
        given = data.get("url_hash")
        if data.get("kind") == "exchange":
            if given is not None:
                if not (isinstance(given, str) and _HEX64.fullmatch(given)):
                    raise ValueError("url_hash must be 64 lower-case hex characters")
                return data
            title, published = data.get("title"), data.get("published_at")
            if not isinstance(title, str) or published is None:
                return data  # the field validators report what is missing
            try:
                when = _UTC.validate_python(published)
            except ValidationError:
                return data
            identity = exchange_identity(title, when, data.get("published_at_source") or "feed")
            return {**data, "url_hash": item_key(data["url"], identity)}
        computed = url_hash(data["url"])
        if given not in (None, computed):
            raise ValueError("url_hash does not match the normalised url")
        return {**data, "url_hash": computed}

    @model_validator(mode="before")
    @classmethod
    def _fill_times(cls, data: Any) -> Any:
        # fetched_at's default must exist before available_at can be computed from it.
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("fetched_at") is None:
            data["fetched_at"] = utc_now()
        if data.get("available_at") is None and data.get("published_at") is not None:
            try:
                published = _UTC.validate_python(data["published_at"])
                fetched = _UTC.validate_python(data["fetched_at"])
            except ValidationError:
                return data  # the field validators report the real error
            data["available_at"] = max(published, fetched)
        return data

    @model_validator(mode="after")
    def _check_available_at(self) -> NewsItem:
        if self.available_at != max(self.published_at, self.fetched_at):
            raise ValueError("available_at must be max(published_at, fetched_at)")
        return self


# --- llm_extractions -------------------------------------------------------------------------


class PolicyChange(_Strict):
    present: bool
    direction: Literal["positive", "negative", "neutral"] | None = None


class OrderWin(_Strict):
    present: bool
    amount_inr: float | None = None
    sign: Literal["positive", "negative"] | None = None


class Events(_Strict):
    war_conflict: bool
    policy_change: PolicyChange
    rbi_action: Literal["hike", "cut", "hold", "none"]
    budget_announcement: bool
    order_win: OrderWin
    results_surprise: Literal["beat", "miss", "inline", "none"]
    promoter_activity: Literal["buying", "selling", "none"]
    sentiment: float = Field(ge=-1, le=1)


class LLMExtraction(Document):
    news_item_id: ObjectIdField
    published_at: UtcDatetime  # copied from the news item
    extracted_at: UtcDatetime = Field(default_factory=utc_now)
    provider: str
    model: str
    prompt_version: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_inr: float = Field(ge=0)  # indicative; the exact spend is in Postgres llm_spend
    companies: list[str] = Field(default_factory=list)  # symbols or ISINs
    events: Events


# --- prediction_audit ------------------------------------------------------------------------


class InstrumentRef(_Strict):
    symbol: str
    isin: str | None = None
    instrument_id: int | None = None  # Postgres instruments.id


class ModelRef(_Strict):
    name: str
    version: str


class ShapItem(_Strict):
    feature: str
    value: float | int | str | bool | None = None  # the feature's value for this prediction
    contribution: float  # SHAP value: how much this feature pushed the prediction
    reason_text: str


class NewsUsed(_Strict):
    news_item_id: ObjectIdField
    url: str
    published_at: UtcDatetime


class LLMCallRef(_Strict):
    provider: str
    model: str
    prompt_version: str
    cost_inr: float = Field(ge=0)


class Outcome(_Strict):
    filled_at: UtcDatetime
    actual_return_pct: float
    actual_direction: Signal
    error: float | None = None  # predicted minus actual return, if a return was predicted
    correct: bool


class LeakageError(ValueError):
    """The audit uses information from after the prediction time."""


class PredictionAudit(Document):
    prediction_id: UuidStr = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: UtcDatetime  # the prediction time
    instrument: InstrumentRef
    model: ModelRef
    horizon: Horizon
    data_source: DataSource
    features: dict[str, Any]  # snapshot of the inputs, as used
    feature_cutoff: UtcDatetime  # time of the latest data used
    shap_top: list[ShapItem] = Field(default_factory=list)
    news_events_used: list[NewsUsed] = Field(default_factory=list)
    llm_calls: list[LLMCallRef] = Field(default_factory=list)
    signal: Signal
    predicted_return_pct: float | None = None
    confidence: float = Field(ge=0, le=1)
    outcome_status: Literal["pending", "filled"] = "pending"
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def _guards(self) -> PredictionAudit:
        # Early leakage guard: nothing the prediction used may be newer than the prediction.
        if self.feature_cutoff > self.created_at:
            raise LeakageError(
                f"leakage: feature_cutoff {self.feature_cutoff.isoformat()} is after "
                f"created_at {self.created_at.isoformat()}"
            )
        for news in self.news_events_used:
            if news.published_at > self.created_at:
                raise LeakageError(
                    f"leakage: news {news.news_item_id} published_at "
                    f"{news.published_at.isoformat()} is after created_at "
                    f"{self.created_at.isoformat()}"
                )
        if (self.outcome is None) != (self.outcome_status == "pending"):
            raise ValueError("outcome must be set exactly when outcome_status is 'filled'")
        return self
