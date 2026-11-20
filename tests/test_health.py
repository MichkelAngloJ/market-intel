"""market_intel.health with fakes: every check's status logic, isolation, masking, the alert
rule (one on FAIL, none on WARN), exit codes and the JSON shape. No network."""

import copy
import io
import json
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from market_intel import backup, health
from market_intel.config import load_settings
from market_intel.db.common import DbReport
from market_intel.logging_setup import configure_logging
from market_intel.mongo.schemas import COLLECTIONS, VALIDATION_ACTION, VALIDATION_LEVEL

FAKE_PW = "fake-pass-555"
PG_URL = f"postgresql://fake_user:{FAKE_PW}@db.invalid:5432/x?sslmode=require"
MONGO_URL = f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/"
FAKE_ALERT_CREDENTIAL = "fake-alert-credential-555"
ALERTS = {
    "ALERT_EMAIL_FROM": "alerts.sender@example.com",
    "ALERT_EMAIL_TO": "me.main@example.com",
    "ALERT_EMAIL_APP_PASSWORD": FAKE_ALERT_CREDENTIAL,
}
Usage = namedtuple("Usage", "total used free")
GB = 1024**3


# --- fakes -----------------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakePg:
    def __init__(self, versions=("0003",), last_run=None, spend=None):
        self.versions = versions
        self.last_run = last_run
        self.spend = spend
        self.closed = False

    def execute(self, sql):
        if "alembic_version" in sql:
            return FakeResult([(v,) for v in self.versions])
        if "ingestion_runs" in sql:
            return FakeResult([self.last_run] if self.last_run else [])
        if "v_llm_spend_monthly" in sql:
            return FakeResult([(self.spend,)] if self.spend is not None else [])
        raise AssertionError(sql)

    def close(self):
        self.closed = True


class FakeScripts:
    def __init__(self, heads=("0003",), known=("0001", "0002", "0003")):
        self.heads, self.known = list(heads), set(known)

    def get_heads(self):
        return self.heads

    def get_revision(self, rev):
        if rev not in self.known:
            raise RuntimeError(f"Can't locate revision {rev}")
        return object()


def refused(code=8000):
    return OperationFailure("not authorized", code=code)


class FakeColl:
    def __init__(self, indexes, on_delete=None, on_insert=None):
        self.indexes = indexes
        self.on_delete = on_delete
        self.on_insert = on_insert
        self.deleted_filters = []

    def index_information(self):
        return copy.deepcopy(self.indexes)

    def delete_many(self, flt):
        self.deleted_filters.append(flt)
        if self.on_delete:
            raise self.on_delete
        return SimpleNamespace(deleted_count=0)

    def insert_one(self, doc):
        if self.on_insert:
            raise self.on_insert
        return SimpleNamespace(inserted_id=1)


class FakeMongoDb:
    """A database exactly as mongo_setup would leave it, with the Atlas role's behaviour."""

    def __init__(self, *, extra=(), missing=(), delete_errors=None, insert_error="refused"):
        self.options, self.colls = {}, {}
        delete_errors = {"prediction_audit": refused(), **(delete_errors or {})}
        for spec in COLLECTIONS:
            if spec.name in missing:
                continue
            opts = {}
            if spec.validator:
                opts = {
                    "validator": spec.validator,
                    "validationLevel": VALIDATION_LEVEL,
                    "validationAction": VALIDATION_ACTION,
                }
            self.options[spec.name] = opts
            indexes = {"_id_": {"key": [("_id", 1)]}}
            for idx in spec.indexes:
                indexes[idx.name] = {
                    "key": list(idx.keys),
                    **({"unique": True} if idx.unique else {}),
                }
            self.colls[spec.name] = FakeColl(indexes, on_delete=delete_errors.get(spec.name))
        for name in extra:
            self.options[name] = {}
            self.colls[name] = FakeColl({})
        self.insert_error = refused() if insert_error == "refused" else insert_error
        self.created = []

    def list_collections(self):
        return [{"name": n, "options": copy.deepcopy(o)} for n, o in self.options.items()]

    def list_collection_names(self):
        return list(self.options)

    def __getitem__(self, name):
        if name not in self.colls:  # the probe's unknown collection
            coll = FakeColl({}, on_insert=self.insert_error)
            if self.insert_error is None:
                self.created.append(name)
            return coll
        return self.colls[name]


class FakeClient:
    def __init__(self, db):
        self.db = db
        self.closed = False

    def __getitem__(self, name):
        return self.db

    def close(self):
        self.closed = True


def ok_report(name, **overrides):
    fields = dict(
        name=name,
        host=f"{name}.invalid",
        reachable=True,
        version="1",
        latency_ms=10.0,
        tls=True,
        used_mb=10.0,
        limit_mb=1000,
        write_ok=True,
    )
    return DbReport(**{**fields, **overrides})


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, level, title, body, **kwargs):
        self.sent.append((level, title, body, kwargs))
        return SimpleNamespace(status="dry_run")


def write_manifest(folder, taken, **fields):
    """A manifest named by its UTC timestamp, as market_intel.backup writes it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / backup.MANIFEST.name(backup.stamp(taken))
    data = {"format": 1, "created_utc": taken.strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    # A fresh complete backup in a tmp BACKUP_DIR: the real backup folder is never read.
    write_manifest(tmp_path / "backups", datetime.now(UTC) - timedelta(hours=1))
    values = {
        "APP_PROFILE": "full",
        "LOG_DIR": str(tmp_path / "logs"),
        "DATA_DIR": str(tmp_path / "data"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "POSTGRES_URL": PG_URL,
        "MONGODB_URI": MONGO_URL,
        "ALERT_DRY_RUN": "true",
        **ALERTS,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def make_ctx(
    *,
    pg=None,
    mongo_db=None,
    scripts=None,
    pg_report=None,
    mongo_report=None,
    free_gb=100,
    pg_connect=None,
):
    settings = load_settings()
    configure_logging(settings, console_stream=io.StringIO())
    pg = pg or FakePg()
    mongo_db = mongo_db or FakeMongoDb()
    return health.Context(
        lambda: settings,
        pg_checker=lambda _db: pg_report or ok_report("postgres"),
        mongo_checker=lambda _db: mongo_report or ok_report("mongodb"),
        pg_connect=pg_connect or (lambda _db: pg),
        mongo_client=lambda _db: FakeClient(mongo_db),
        script_directory=lambda: scripts or FakeScripts(),
        disk_usage=lambda _p: Usage(500 * GB, 0, free_gb * GB),
        dir_size=lambda _p: 1024 * 1024,
        probe_id=lambda: "a" * 32,
    )


def by_name(results):
    return {r.name: r for r in results}


# --- the whole run ---------------------------------------------------------------------------


def test_all_good_statuses(env):
    results = by_name(health.run_checks(make_ctx()))
    assert {n: r.status for n, r in results.items()} == {
        "config": "OK",
        "config_upstox": "SKIP",
        "config_llm": "SKIP",
        "databases": "OK",
        "pg_migrations": "OK",
        "mongo_setup_drift": "OK",
        "mongo_rights_probe": "OK",
        "unexpected_collections": "OK",
        "laptop": "OK",
        "backup_age": "OK",
        "data_freshness": "SKIP",
        "upstox_token": "SKIP",
        "llm_spend": "OK",
    }
    assert results["pg_migrations"].message == "at 0003 (head)"
    assert results["config_upstox"].message == "not configured yet"
    assert results["upstox_token"].message == "not set up (Phase 1)"


def test_exit_0_and_no_alert_when_only_warn(env, capsys):
    sender = FakeSender()
    ctx = make_ctx(free_gb=5)  # low disk: WARN only
    assert health.main([], ctx=ctx, sender=sender) == 0
    out = capsys.readouterr().out
    assert "WARN    laptop" in out and "overall: WARN" in out
    assert sender.sent == []


def test_exit_1_and_exactly_one_alert_on_fail(env, capsys):
    sender = FakeSender()
    ctx = make_ctx(
        pg_report=DbReport(name="postgres", host="db.invalid", error="OperationalError: x"),
        scripts=FakeScripts(heads=("0003",), known=("0001",)),
        pg=FakePg(versions=("0009",)),
    )
    assert health.main([], ctx=ctx, sender=sender) == 1
    assert len(sender.sent) == 1
    level, title, body, kwargs = sender.sent[0]
    assert (level, title, kwargs["key"]) == ("critical", "Health check: 2 failing", "health_fail")
    assert "- databases:" in body and "- pg_migrations:" in body
    assert "alert: critical -> dry_run" in capsys.readouterr().out


def test_real_sender_in_dry_run(env):
    results = [health.CheckResult("databases", "FAIL", "x")]
    assert health.alert_on_failures(results, load_settings(), None) == "dry_run"


def test_alerts_not_configured_does_not_crash(env, monkeypatch):
    for key in ALERTS:
        monkeypatch.delenv(key)
    results = [health.CheckResult("databases", "FAIL", "x")]
    assert health.alert_on_failures(results, load_settings(), None) == "not_configured"


def test_a_crashing_check_fails_alone_and_is_masked(env):
    def boom(_db):
        raise RuntimeError(f"cannot connect to {PG_URL}")

    results = by_name(health.run_checks(make_ctx(pg_connect=boom)))
    for name in ("pg_migrations", "data_freshness", "llm_spend"):
        assert results[name].status == "FAIL"
        assert FAKE_PW not in results[name].message and "fake_user" not in results[name].message
    assert results["mongo_rights_probe"].status == "OK"  # the others still ran
    assert len(results) == len(health.CHECKS)


def test_json_shape_and_no_secrets(env, capsys):
    assert health.main(["--json"], ctx=make_ctx(), sender=FakeSender()) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload) == {"generated_at", "overall", "counts", "alert", "checks"}
    assert payload["generated_at"].endswith("+00:00")
    assert payload["overall"] == "OK" and payload["alert"] is None
    assert [c["name"] for c in payload["checks"]] == [name for name, _ in health.CHECKS]
    for check in payload["checks"]:
        assert set(check) == {"name", "status", "message", "details"}
    for secret in (FAKE_PW, "fake_user", FAKE_ALERT_CREDENTIAL, "me.main@example.com"):
        assert secret not in out


def test_overall_and_counts():
    r = [health.CheckResult("a", s, "") for s in ("OK", "SKIP", "WARN")]
    assert health.overall(r) == "WARN"
    assert health.overall(r + [health.CheckResult("b", "FAIL", "")]) == "FAIL"
    assert health.overall([health.CheckResult("a", "SKIP", "")]) == "OK"
    assert health.counts(r) == {"OK": 1, "WARN": 1, "FAIL": 0, "SKIP": 1}


# --- config ----------------------------------------------------------------------------------


def test_config_partial_group_warns(env):
    env.setenv("UPSTOX_API_KEY", "fake-upstox-key")
    assert health.run_checks(make_ctx())[1].status == "WARN"


def test_config_alerts_missing_warns(env):
    for key in ALERTS:
        env.delenv(key)
    result = health.check_config(make_ctx())
    assert result.status == "WARN" and "ALERT_EMAIL_FROM" in result.message


def test_config_missing_database_fails(env):
    env.delenv("POSTGRES_URL")
    results = by_name(health.run_checks(make_ctx()))
    assert results["config"].status == "FAIL" and "POSTGRES_URL" in results["config"].message
    assert results["databases"].status == "FAIL"


# --- databases -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "status", "text"),
    [
        (
            DbReport(name="postgres", host="h.invalid", error="OperationalError: x"),
            "FAIL",
            "unreachable",
        ),
        (
            DbReport(name="postgres", host="h.invalid", refused=True, error="refusing"),
            "FAIL",
            "no TLS",
        ),
        (ok_report("postgres", tls=False), "FAIL", "no TLS"),
        (ok_report("postgres", tls=False, host="127.0.0.1"), "OK", "postgres ok"),
        (ok_report("postgres", write_ok=False), "FAIL", "write test failed"),
        (ok_report("postgres", used_mb=850.0), "WARN", "storage at 85%"),
    ],
)
def test_databases_status(env, report, status, text):
    result = health.check_databases(make_ctx(pg_report=report))
    assert result.status == status and text in result.message


# --- migrations ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("versions", "status", "text"),
    [
        (("0003",), "OK", "at 0003 (head)"),
        (("0002",), "WARN", "run alembic upgrade head"),
        ((), "WARN", "no migration applied"),
        (("0999",), "FAIL", "doesn't know"),
    ],
)
def test_pg_migrations(env, versions, status, text):
    result = health.check_pg_migrations(make_ctx(pg=FakePg(versions=versions)))
    assert result.status == status and text in result.message


# --- mongo -----------------------------------------------------------------------------------


def test_mongo_setup_drift_missing_collection_warns(env):
    result = health.check_mongo_setup_drift(make_ctx(mongo_db=FakeMongoDb(missing=("news_items",))))
    assert result.status == "WARN" and "run mongo_setup" in result.message
    assert "news_items: collection with validator" in result.details["differences"]


def test_mongo_setup_drift_old_validator_warns(env):
    db = FakeMongoDb()
    db.options["prediction_audit"]["validator"] = {"$jsonSchema": {}}
    assert health.check_mongo_setup_drift(make_ctx(mongo_db=db)).status == "WARN"


@pytest.mark.parametrize("code", [8000, 13])
def test_probe_ok_with_either_refusal_code(env, code):
    db = FakeMongoDb(delete_errors={"prediction_audit": refused(code)}, insert_error=refused(code))
    result = health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert result.status == "OK"
    assert result.details["delete prediction_audit"] == f"refused ({code})"
    assert result.details["delete news_items"] == "allowed"


def test_probe_deletes_match_nothing(env):
    db = FakeMongoDb()
    health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert db.colls["news_items"].deleted_filters == [{"_health_probe": "a" * 32}]


def test_probe_fails_if_audit_delete_allowed(env):
    db = FakeMongoDb(delete_errors={"prediction_audit": None})
    result = health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert (
        result.status == "FAIL" and "prediction_audit allowed, expected refused" in result.message
    )


def test_probe_fails_if_news_delete_refused(env):
    db = FakeMongoDb(delete_errors={"news_items": refused(13)})
    result = health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert result.status == "FAIL" and "news_items refused, expected allowed" in result.message


def test_probe_other_error_code_warns(env):
    db = FakeMongoDb(delete_errors={"llm_extractions": OperationFailure("odd", code=2)})
    result = health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert result.status == "WARN" and "error code 2" in result.message


def test_probe_network_error_warns(env):
    db = FakeMongoDb(insert_error=ServerSelectionTimeoutError("timeout"))
    assert health.check_mongo_rights_probe(make_ctx(mongo_db=db)).status == "WARN"


def test_probe_unexpected_insert_success_fails_and_names_the_collection(env):
    db = FakeMongoDb(insert_error=None)
    result = health.check_mongo_rights_probe(make_ctx(mongo_db=db))
    assert result.status == "FAIL"
    assert db.created == ["zz_health_probe_aaaaaaaaaaaa"]
    assert "zz_health_probe_aaaaaaaaaaaa" in result.message and "drop" in result.message


def test_unexpected_collections_warns(env):
    result = health.check_unexpected_collections(make_ctx(mongo_db=FakeMongoDb(extra=("zz_x",))))
    assert result.status == "WARN" and "zz_x" in result.message


# --- laptop, freshness, spend ---------------------------------------------------------------


@pytest.mark.parametrize(("free_gb", "status"), [(50, "OK"), (9.9, "WARN"), (1.5, "FAIL")])
def test_laptop_disk_thresholds(env, free_gb, status):
    result = health.check_laptop(make_ctx(free_gb=free_gb))
    assert result.status == status
    assert result.details["logs_mb"] == 1.0 and result.details["data_mb"] == 1.0


def test_data_freshness(env):
    from datetime import UTC, datetime

    t = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
    failed = ("daily_candles", "yfinance", "failed", t, t)
    ok = ("daily_candles", "yfinance", "success", t, t)
    assert health.check_data_freshness(make_ctx(pg=FakePg(last_run=failed))).status == "WARN"
    result = health.check_data_freshness(make_ctx(pg=FakePg(last_run=ok)))
    assert result.status == "OK" and "2026-11-18T10:00:00+00:00" in result.message


@pytest.mark.parametrize(
    ("spend", "status"), [(None, "OK"), (500, "OK"), (800, "WARN"), (1000, "FAIL")]
)
def test_llm_spend(env, spend, status):
    from decimal import Decimal

    pg = FakePg(spend=None if spend is None else Decimal(spend))
    assert health.check_llm_spend(make_ctx(pg=pg)).status == status


def test_connections_are_closed(env):
    pg, db = FakePg(), FakeMongoDb()
    ctx = make_ctx(pg=pg, mongo_db=db)
    health.main([], ctx=ctx, sender=FakeSender())
    assert pg.closed


# --- task 0.10: backup age ---------------------------------------------------------------


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def backup_ctx(now=NOW):
    ctx = make_ctx()
    ctx.now = lambda: now
    return ctx


@pytest.fixture
def backups(env, tmp_path):
    """An empty BACKUP_DIR (the env fixture's fresh manifest removed)."""
    folder = tmp_path / "backups"
    for path in folder.iterdir():
        path.unlink()
    return folder


def test_backup_age_ok_when_recent(backups):
    write_manifest(backups, NOW - timedelta(days=2))
    result = health.check_backup_age(backup_ctx())
    assert result.status == "OK"
    assert result.message == "newest backup 2.0 days old (limit 7 days)"
    assert result.details["created_utc"] == "2026-11-19T12:00:00Z"


def test_backup_age_uses_the_newest_manifest(backups):
    write_manifest(backups, NOW - timedelta(days=30))
    write_manifest(backups, NOW - timedelta(hours=5))
    result = health.check_backup_age(backup_ctx())
    assert result.status == "OK" and "5 h old" in result.message


def test_backup_age_warns_when_old(backups):
    write_manifest(backups, NOW - timedelta(days=7, minutes=1))
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN"
    assert (
        result.message
        == "newest backup 7.0 days old (limit 7 days): run python -m market_intel.backup"
    )


def test_backup_age_limit_comes_from_settings(backups, monkeypatch):
    write_manifest(backups, NOW - timedelta(days=2))
    monkeypatch.setenv("BACKUP_MAX_AGE_DAYS", "1")
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "limit 1 days" in result.message


def test_backup_age_warns_when_no_folder(env, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "never-created"))
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "no backup folder" in result.message


def test_backup_age_warns_when_no_manifest(backups):
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "no complete backup" in result.message


def test_backup_age_ignores_a_dump_without_a_manifest(backups):
    write_manifest(backups, NOW - timedelta(days=10))
    fresh = backup.stamp(NOW - timedelta(minutes=5))
    for kind in backup.DUMPS:  # a newer backup that never finished: dumps, no manifest
        (backups / kind.name(fresh)).write_bytes(b"x")
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "10.0 days old" in result.message
    for path in backups.iterdir():
        if backup.MANIFEST.matches(path.name):
            path.unlink()
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "no complete backup" in result.message


def test_backup_age_warns_on_an_unreadable_manifest(backups):
    path = write_manifest(backups, NOW - timedelta(days=1))
    path.write_text("not json", encoding="utf-8")
    result = health.check_backup_age(backup_ctx())
    assert result.status == "WARN" and "no readable timestamp" in result.message


@pytest.mark.parametrize("profile", ["restricted", "server"])
def test_backup_age_skips_off_the_full_profile(backups, monkeypatch, profile):
    monkeypatch.setenv("APP_PROFILE", profile)
    result = health.check_backup_age(backup_ctx())
    assert result.status == "SKIP" and result.message == "backups run on the full profile only"


def test_backup_age_never_touches_a_database(backups):
    write_manifest(backups, NOW - timedelta(days=1))

    def no_db(_db):
        raise AssertionError("backup_age must not open a database")

    ctx = make_ctx(pg_connect=no_db)
    ctx._mongo_client = no_db
    ctx.now = lambda: NOW
    assert health.check_backup_age(ctx).status == "OK"
