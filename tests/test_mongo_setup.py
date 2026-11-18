"""mongo_setup with a fake database: create, update, idempotence, conflicts, never drop."""

import copy

import pymongo
import pytest

from market_intel import mongo_setup
from market_intel.mongo.schemas import COLLECTIONS, NEWS_ITEMS, PREDICTION_AUDIT

FAKE_PW = "fake-pass-987"


class FakeCollection:
    def __init__(self):
        self.indexes = {"_id_": {"key": [("_id", 1)], "v": 2}}

    def index_information(self):
        return copy.deepcopy(self.indexes)

    def create_index(self, keys, name, unique=False):
        info = {"key": list(keys), "v": 2}
        if unique:
            info["unique"] = True
        self.indexes[name] = info
        return name

    def __getattr__(self, name):  # drop_index, drop, ... must never be called
        raise AssertionError(f"mongo_setup called {name}")


class FakeDb:
    name = "market_intel"

    def __init__(self):
        self.options: dict[str, dict] = {}
        self.colls: dict[str, FakeCollection] = {}
        self.calls: list[str] = []

    def list_collections(self):
        return [{"name": n, "options": copy.deepcopy(o)} for n, o in self.options.items()]

    def create_collection(self, name, **options):
        self.calls.append(f"create {name}")
        self.options[name] = options
        self.colls[name] = FakeCollection()

    def command(self, cmd, name, **options):
        assert cmd == "collMod"
        self.calls.append(f"collMod {name}")
        self.options[name].update(options)

    def __getitem__(self, name):
        return self.colls[name]

    def __getattr__(self, name):  # drop_collection, ... must never be called
        raise AssertionError(f"mongo_setup called {name}")


def test_fresh_database_gets_everything_then_nothing_to_change(capsys):
    db = FakeDb()
    assert mongo_setup.run(db, "db.invalid") == 0
    first = capsys.readouterr().out
    assert set(db.options) == {c.name for c in COLLECTIONS}
    assert db.options["news_items"]["validator"] == NEWS_ITEMS.validator
    assert db.options["news_items"]["validationLevel"] == "strict"
    assert db.options["news_items"]["validationAction"] == "error"
    assert "validator" not in db.options["_healthcheck"]
    assert db.colls["news_items"].indexes["uq_url_hash"]["unique"] is True
    n_indexes = sum(len(c.indexes) for c in COLLECTIONS)
    assert f"summary: created {len(COLLECTIONS) + n_indexes}, updated 0" in first

    db.calls.clear()
    assert mongo_setup.run(db, "db.invalid") == 0
    assert "nothing to change" in capsys.readouterr().out
    assert db.calls == []


def test_existing_healthcheck_is_kept_and_old_validator_updated(capsys):
    db = FakeDb()
    db.create_collection("_healthcheck")
    db.create_collection("prediction_audit", validator={"$jsonSchema": {"required": []}})
    db.calls.clear()
    assert mongo_setup.run(db, "db.invalid") == 0
    assert "collMod prediction_audit" in db.calls
    assert "create _healthcheck" not in db.calls
    assert db.options["prediction_audit"]["validator"] == PREDICTION_AUDIT.validator
    assert "updated  prediction_audit: validator (collMod)" in capsys.readouterr().out


def test_index_with_same_name_but_other_definition_stops_before_any_change(capsys):
    db = FakeDb()
    db.create_collection("news_items")
    db.colls["news_items"].create_index([("url", 1)], name="uq_url_hash", unique=True)
    db.calls.clear()
    assert mongo_setup.run(db, "db.invalid") == 1
    out = capsys.readouterr().out
    assert "CONFLICT news_items: index uq_url_hash: exists with a different definition" in out
    assert "nothing was changed" in out
    assert db.calls == []  # no collection created, no collMod


def test_same_keys_under_another_name_is_a_conflict(capsys):
    db = FakeDb()
    db.create_collection("news_items")
    db.colls["news_items"].create_index([("published_at", -1)], name="published_at_-1")
    assert mongo_setup.run(db, "db.invalid") == 1
    assert "already indexed as published_at_-1" in capsys.readouterr().out


def test_main_needs_the_setup_login(monkeypatch, capsys):
    monkeypatch.setenv("APP_PROFILE", "full")
    assert mongo_setup.main() == 1
    assert "MONGODB_ADMIN_URI" in capsys.readouterr().err


def test_main_refuses_unencrypted_non_local_without_connecting(monkeypatch, capsys):
    monkeypatch.setenv("APP_PROFILE", "restricted")
    monkeypatch.setenv("MONGODB_ADMIN_URI", f"mongodb://setup:{FAKE_PW}@db.example.com/?tls=false")
    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **k: pytest.fail("connected"))
    assert mongo_setup.main() == 1
    out = capsys.readouterr()
    assert "refusing unencrypted connection" in out.err
    assert FAKE_PW not in out.out + out.err
