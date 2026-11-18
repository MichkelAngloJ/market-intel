"""The single collection helper and db_check's unexpected-collection warning. Fakes only."""

import io

import pytest

from market_intel import db_check
from market_intel.config import load_settings
from market_intel.db import mongo
from market_intel.db.common import DbReport
from market_intel.logging_setup import LOG_FILE_NAME, configure_logging
from market_intel.mongo.access import (
    COLLECTION_NAMES,
    UnknownCollectionError,
    collection,
    unexpected_collections,
)


class NoTouchDb:
    """Fails if the helper touches the database for an unknown name."""

    def __getitem__(self, name):
        pytest.fail(f"database accessed for {name!r}")


class FakeColl:
    def __init__(self):
        self.docs = {}

    def insert_one(self, doc):
        self.docs[doc["_id"]] = doc

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def delete_one(self, query):
        self.docs.pop(query["_id"], None)


class FakeDb:
    def __init__(self, names):
        self.names = names
        self.colls = {}

    def command(self, name):
        return {"ok": 1} if name == "ping" else {"storageSize": 0, "indexSize": 0}

    def list_collection_names(self):
        return list(self.names)

    def __getitem__(self, name):
        return self.colls.setdefault(name, FakeColl())


class FakeClient:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getitem__(self, name):
        return self.db

    def server_info(self):
        return {"version": "8.0.37"}


def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ALERT_DRY_RUN", "true")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@db.invalid/x?sslmode=require")
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://u:p@cluster.invalid/")
    s = load_settings()
    configure_logging(s, console_stream=io.StringIO())
    return s


# --- helper ----------------------------------------------------------------------------------


def test_collection_returns_known_collections():
    db = FakeDb([])
    for name in COLLECTION_NAMES:
        assert collection(db, name) is db[name]


@pytest.mark.parametrize("name", ["news_item", "zz_cc_probe", "system.users", ""])
def test_unknown_collection_raises_before_touching_the_database(name):
    with pytest.raises(UnknownCollectionError, match="unknown collection"):
        collection(NoTouchDb(), name)


def test_known_names_are_the_four_collections():
    assert COLLECTION_NAMES == {"news_items", "llm_extractions", "prediction_audit", "_healthcheck"}


def test_unexpected_collections_ignores_system_collections():
    names = ["news_items", "zz_probe", "system.views", "_healthcheck", "abc"]
    assert unexpected_collections(names) == ["abc", "zz_probe"]


# --- db_check warning ------------------------------------------------------------------------


def test_mongo_check_warns_about_unexpected_collections_but_passes(tmp_path, monkeypatch):
    db = settings(tmp_path, monkeypatch).require("database")
    fake = FakeDb(["news_items", "_healthcheck", "zz_cc_probe"])
    report = mongo.check(db, client_factory=lambda _db: FakeClient(fake))
    assert report.ok
    assert report.warnings == ["unexpected collections (not created by mongo_setup): zz_cc_probe"]


def test_mongo_check_no_warning_when_only_known_collections(tmp_path, monkeypatch):
    db = settings(tmp_path, monkeypatch).require("database")
    fake = FakeDb(sorted(COLLECTION_NAMES))
    assert mongo.check(db, client_factory=lambda _db: FakeClient(fake)).warnings == []


def test_db_check_prints_and_logs_warning_without_failing(tmp_path, monkeypatch, capsys):
    s = settings(tmp_path, monkeypatch)
    warned = DbReport(
        name="mongodb",
        host="cluster.invalid",
        reachable=True,
        version="8.0.37",
        latency_ms=1.0,
        tls=True,
        used_mb=0.0,
        limit_mb=512,
        write_ok=True,
        warnings=["unexpected collections (not created by mongo_setup): zz_cc_probe"],
    )
    assert db_check.run(s, checkers={"mongodb": lambda _db: warned}) == 0
    out = capsys.readouterr().out
    assert "warning   : unexpected collections (not created by mongo_setup): zz_cc_probe" in out
    assert "alert" not in out and "result: OK" in out
    logs = (s.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "db_check_warning" in logs and "zz_cc_probe" in logs
