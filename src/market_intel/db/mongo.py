"""MongoDB via PyMongo (sync API; Motor is retired in favour of PyMongo's own async API)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pymongo
from bson import ObjectId

from market_intel.config import DatabaseSettings
from market_intel.db.common import (
    REFUSAL,
    DbReport,
    InsecureConnectionError,
    default_clock,
    host_is_local,
    median_latency_ms,
    parse_url,
    query_params,
    redactor,
)
from market_intel.mongo.access import collection, unexpected_collections

APP_NAME = "market-intel"
HEALTHCHECK_COLLECTION = "_healthcheck"


TLS_HINT = "use mongodb+srv:// or tls=true"


def ensure_tls(url: str, profile: str) -> None:
    """Raise InsecureConnectionError if ``url`` would reach a non-local host without TLS.

    Runs before a client exists, so the password is never sent in plain text.
    """
    host = parse_url(url).hosts
    if url_tls(url) is not True and not host_is_local(host, profile):
        raise InsecureConnectionError(f"{REFUSAL} {host} ({TLS_HINT})")


def client(db: DatabaseSettings) -> pymongo.MongoClient:
    """App client (MONGODB_URI). See :func:`client_url`."""
    return client_url(
        db.mongodb_uri.get_secret_value(),
        profile=db.app_profile,
        timeout_s=db.db_connect_timeout_s,
    )


def client_url(url: str, *, profile: str, timeout_s: int) -> pymongo.MongoClient:
    """A client that returns UTC-aware datetimes. Connects lazily on first use.

    Every MongoDB client in the project comes from here (the app and mongo_setup); it refuses
    unencrypted non-local connections first (:func:`ensure_tls`).
    """
    ensure_tls(url, profile)
    timeout_ms = timeout_s * 1000
    return pymongo.MongoClient(
        url,
        appname=APP_NAME,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        tz_aware=True,
        tzinfo=UTC,
    )


def url_tls(url: str) -> bool | None:
    """TLS as set by the connection string; None if the scheme is not a MongoDB one.

    mongodb+srv:// turns TLS on unless tls=false or ssl=false; mongodb:// only with
    tls=true or ssl=true (the same rules PyMongo applies).
    """
    params = query_params(url)
    flags = {params.get("tls", "").lower(), params.get("ssl", "").lower()}
    if url.startswith("mongodb+srv://"):
        return "false" not in flags
    if url.startswith("mongodb://"):
        return "true" in flags
    return None


def tls_enabled(mongo: pymongo.MongoClient) -> bool | None:
    """Fallback only: PyMongo has no public TLS flag, so read the pool's private SSL context.
    Returns None if a PyMongo version changes that attribute."""
    try:
        return mongo.options.pool_options._ssl_context is not None
    except Exception:
        return None


def check(
    db: DatabaseSettings,
    *,
    client_factory: Callable[[DatabaseSettings], pymongo.MongoClient] = client,
    clock: Callable[[], float] = default_clock,
) -> DbReport:
    """Reachability, version, latency, client-side TLS, storage and a write test."""
    url = db.mongodb_uri.get_secret_value()
    redact = redactor(url)
    report = DbReport(name="mongodb", host=parse_url(url).hosts, limit_mb=db.mongo_storage_limit_mb)
    tls = url_tls(url)
    try:
        ensure_tls(url, db.app_profile)  # also inside client(); here for custom factories
    except InsecureConnectionError:
        report.tls, report.refused = False, True
        report.error = f"{REFUSAL} ({TLS_HINT})"
        return report
    try:
        with client_factory(db) as mongo:
            database = mongo[db.mongodb_db]
            database.command("ping")  # forces server selection; raises if unreachable
            report.reachable = True
            report.version = mongo.server_info()["version"]
            report.latency_ms = median_latency_ms(lambda: database.command("ping"), clock)
            report.tls = tls if tls is not None else tls_enabled(mongo)
            stats = database.command("dbStats")
            # On-disk size incl. indexes: the conservative measure against the free limit.
            used = stats.get("storageSize", 0) + stats.get("indexSize", 0)
            report.used_mb = round(used / 1024 / 1024, 1)
            report.write_ok = _write_test(database)
            report.warnings += _collection_warnings(database)
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {redact(str(exc)).strip()}"
    return report


def _collection_warnings(database) -> list[str]:
    """Warn (don't fail) about collections that aren't in the list in schemas.py."""
    try:
        extra = unexpected_collections(database.list_collection_names())
    except Exception as exc:
        return [f"could not list collections ({type(exc).__name__})"]
    if not extra:
        return []
    return [f"unexpected collections (not created by mongo_setup): {', '.join(extra)}"]


def _write_test(database) -> bool:
    coll = collection(database, HEALTHCHECK_COLLECTION)
    doc_id = ObjectId()
    try:
        coll.insert_one({"_id": doc_id, "at": datetime.now(UTC), "note": "ok"})
        found = coll.find_one({"_id": doc_id})
        return found is not None and found["note"] == "ok"
    finally:
        try:
            coll.delete_one({"_id": doc_id})  # never leave test data behind
        except Exception:
            pass  # if the insert failed, the delete fails too; keep the original error
