"""MongoDB via PyMongo (sync API; Motor is retired in favour of PyMongo's own async API)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pymongo
from bson import ObjectId

from market_intel.config import DatabaseSettings
from market_intel.db.common import DbReport, default_clock, median_latency_ms, parse_url, redactor

APP_NAME = "market-intel"
HEALTHCHECK_COLLECTION = "_healthcheck"


def client(db: DatabaseSettings) -> pymongo.MongoClient:
    """A client that returns UTC-aware datetimes. Connects lazily on first use."""
    timeout_ms = db.db_connect_timeout_s * 1000
    return pymongo.MongoClient(
        db.mongodb_uri.get_secret_value(),
        appname=APP_NAME,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        tz_aware=True,
        tzinfo=UTC,
    )


def tls_enabled(mongo: pymongo.MongoClient) -> bool:
    """Client-side TLS setting. PyMongo has no public flag; the pool's SSL context is set
    exactly when TLS is on (mongodb+srv:// URLs turn it on by default)."""
    return getattr(mongo.options.pool_options, "_ssl_context", None) is not None


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
    try:
        with client_factory(db) as mongo:
            database = mongo[db.mongodb_db]
            database.command("ping")  # forces server selection; raises if unreachable
            report.reachable = True
            report.version = mongo.server_info()["version"]
            report.latency_ms = median_latency_ms(lambda: database.command("ping"), clock)
            report.tls = tls_enabled(mongo)
            stats = database.command("dbStats")
            # On-disk size incl. indexes: the conservative measure against the free limit.
            used = stats.get("storageSize", 0) + stats.get("indexSize", 0)
            report.used_mb = round(used / 1024 / 1024, 1)
            report.write_ok = _write_test(database)
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {redact(str(exc)).strip()}"
    return report


def _write_test(database) -> bool:
    coll = database[HEALTHCHECK_COLLECTION]
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
