"""Tests for the database checks and db_check command. Fakes only: no network."""

import io
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pymongo
import pytest

from market_intel import db_check
from market_intel.config import ConfigError, Settings, load_settings
from market_intel.db import mongo, postgres
from market_intel.db.common import DbReport, parse_url, redactor
from market_intel.logging_setup import LOG_FILE_NAME, configure_logging

FAKE_PW = "fake-pass-123"
FAKE_USER = "fake_user"
PG_URL = f"postgresql://{FAKE_USER}:{FAKE_PW}@db.invalid:5432/x?sslmode=require"
FAKE_ALERT_CREDENTIAL = "fake-alert-credential"
MONGO_URL = f"mongodb+srv://{FAKE_USER}:{FAKE_PW}@cluster.invalid/?retryWrites=true"
ALERT_ENV = {
    "ALERT_EMAIL_FROM": "alerts.sender@example.com",
    "ALERT_EMAIL_TO": "me.main@example.com",
    "ALERT_EMAIL_APP_PASSWORD": FAKE_ALERT_CREDENTIAL,
}


def make_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, alerts: bool = True, **env: str
) -> Settings:
    base = {
        "APP_PROFILE": "hp",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": PG_URL,
        "MONGODB_URI": MONGO_URL,
        "ALERT_DRY_RUN": "true",
        **(ALERT_ENV if alerts else {}),
    }
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, console_stream=io.StringIO())
    return settings


def log_text(settings: Settings) -> str:
    return (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def ok_report(name: str, **overrides) -> DbReport:
    fields = dict(
        name=name,
        host=f"{name}.invalid",
        reachable=True,
        version="18.7" if name == "postgres" else "8.0.37",
        latency_ms=12.5,
        tls=True,
        used_mb=7.4,
        limit_mb=1024,
        write_ok=True,
    )
    return DbReport(**{**fields, **overrides})


# --- fakes -------------------------------------------------------------------------------


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakePgConn:
    """Answers the queries postgres.check runs."""

    def __init__(self, ssl: bool = True, size_bytes: int = 7_759_462):
        self.pgconn = type("PGconn", (), {"ssl_in_use": ssl})()
        self.size_bytes = size_bytes
        self.queries: list[str] = []
        self.temp_rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def transaction(self):
        yield
        self.temp_rows.clear()  # ON COMMIT DROP

    def execute(self, sql: str):
        self.queries.append(sql)
        if sql.startswith("SHOW server_version"):
            return FakeResult(("18.7 (a21afb5)",))
        if sql.startswith("SELECT pg_database_size"):
            return FakeResult((self.size_bytes,))
        if sql.startswith("INSERT INTO _healthcheck"):
            self.temp_rows.append("ok")
        if sql.startswith("SELECT note FROM _healthcheck"):
            return FakeResult((self.temp_rows[0],) if self.temp_rows else None)
        return FakeResult((1,))


class FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    def insert_one(self, doc):
        self.docs[doc["_id"]] = doc

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def delete_one(self, query):
        self.docs.pop(query["_id"], None)


class FakeMongoDb:
    def __init__(self, storage: int = 0, index: int = 0):
        self.stats = {"storageSize": storage, "indexSize": index}
        self.collections: dict[str, FakeCollection] = {}

    def command(self, name):
        return {"ok": 1} if name == "ping" else self.stats

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    def __init__(self, database: FakeMongoDb, tls: bool = True):
        self.database = database
        self.options = type("Opts", (), {"pool_options": type("Pool", (), {})()})()
        self.options.pool_options._ssl_context = object() if tls else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getitem__(self, name):
        return self.database

    def server_info(self):
        return {"version": "8.0.37"}


# --- url helpers -------------------------------------------------------------------------


def test_parse_url_returns_host_only():
    assert parse_url(PG_URL).hosts == "db.invalid"
    assert parse_url(MONGO_URL).hosts == "cluster.invalid"
    assert parse_url("mongodb://u:p@h1:27017,h2:27018/db").hosts == "h1,h2"


def test_redactor_removes_user_password_and_url():
    redact = redactor(PG_URL)
    text = redact(f'failed for user "{FAKE_USER}" pw {FAKE_PW} url {PG_URL}')
    assert FAKE_USER not in text and FAKE_PW not in text and "postgresql://" not in text


# --- postgres ----------------------------------------------------------------------------


def test_postgres_check_success(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")
    conn = FakePgConn()
    report = postgres.check(db, connector=lambda _db: conn)
    assert report.ok
    assert (report.host, report.version, report.tls) == ("db.invalid", "18.7 (a21afb5)", True)
    assert report.used_mb == 7.4 and report.limit_mb == 1024
    assert report.write_ok is True
    assert any("CREATE TEMP TABLE" in q and "ON COMMIT DROP" in q for q in conn.queries)
    assert sum(q == "SELECT 1" for q in conn.queries) == 3  # median of 3 pings


def test_postgres_connect_retries_once_after_timeout(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")
    calls, sleeps, conn = [], [], object()

    def fake_connect(url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection timeout expired")
        return conn

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert postgres.connect(db, sleep=sleeps.append) is conn
    assert sleeps == [3]
    assert calls[0] == {
        "connect_timeout": 15,
        "application_name": "market-intel",
        "autocommit": True,
    }


def test_postgres_connect_gives_up_after_second_failure(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")

    def always_fail(url, **kwargs):
        raise psycopg.OperationalError("timeout")

    monkeypatch.setattr(psycopg, "connect", always_fail)
    with pytest.raises(psycopg.OperationalError):
        postgres.connect(db, sleep=lambda _s: None)


# --- mongo -------------------------------------------------------------------------------


def test_mongo_check_success_and_cleans_up(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")
    database = FakeMongoDb(storage=40 * 1024 * 1024, index=4 * 1024 * 1024)
    report = mongo.check(db, client_factory=lambda _db: FakeMongoClient(database))
    assert report.ok
    assert (report.host, report.version, report.tls) == ("cluster.invalid", "8.0.37", True)
    assert report.used_mb == 44.0 and report.limit_mb == 512
    assert database.collections["_healthcheck"].docs == {}  # test doc deleted


def test_mongo_client_options(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")
    captured = {}
    monkeypatch.setattr(pymongo, "MongoClient", lambda url, **kw: captured.update(kw) or "c")
    assert mongo.client(db) == "c"
    assert captured["appname"] == "market-intel"
    assert captured["serverSelectionTimeoutMS"] == 15000
    assert captured["tz_aware"] is True


# --- db_check command --------------------------------------------------------------------


def test_run_success_output(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, monkeypatch)
    checkers = {
        "postgres": lambda _db: ok_report("postgres"),
        "mongodb": lambda _db: ok_report("mongodb"),
    }
    assert db_check.run(settings, checkers=checkers) == 0
    out = capsys.readouterr().out
    for part in (
        "reachable : yes",
        "host      : postgres.invalid",
        "version   : 18.7",
        "latency   : 12.5 ms",
        "tls       : yes (client-side)",
        "storage   : 7.4 / 1024 MB (0.7%)",
        "write test: ok",
        "result: OK",
    ):
        assert part in out
    assert "alert" not in out


def test_run_failure_exit_1_and_critical_alert(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, monkeypatch)
    failed = DbReport(name="postgres", host="db.invalid", error="OperationalError: timeout")
    checkers = {"postgres": lambda _db: failed, "mongodb": lambda _db: ok_report("mongodb")}
    assert db_check.run(settings, checkers=checkers) == 1
    out = capsys.readouterr().out
    assert "reachable : NO" in out and "error     : OperationalError: timeout" in out
    assert "alert     : critical -> dry_run" in out
    assert "result: FAILED" in out
    assert "Database unreachable: postgres" in log_text(settings)


def test_storage_over_80_pct_sends_warning(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, monkeypatch)
    full = ok_report("mongodb", used_mb=450.0, limit_mb=512)
    checkers = {"mongodb": lambda _db: full}
    assert db_check.run(settings, checkers=checkers) == 0
    out = capsys.readouterr().out
    assert "WARNING >= 80%" in out
    assert "alert     : warning -> dry_run" in out
    assert "Database storage at 88%: mongodb" in log_text(settings)


def test_alerts_not_configured_does_not_crash(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, monkeypatch, alerts=False)
    failed = DbReport(name="mongodb", host="cluster.invalid", error="ServerSelectionTimeoutError")
    assert db_check.run(settings, checkers={"mongodb": lambda _db: failed}) == 1
    assert "alerts_not_configured" in log_text(settings)
    assert "alert     :" not in capsys.readouterr().out


def test_missing_database_config_is_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APP_PROFILE", "hp")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with pytest.raises(ConfigError, match="POSTGRES_URL, MONGODB_URI"):
        db_check.run(load_settings())
    assert db_check.main() == 1
    assert "POSTGRES_URL" in capsys.readouterr().err


def test_fake_credentials_never_shown(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, monkeypatch)

    def failing_connect(url, **kwargs):
        # libpq-style message that names the user; the URL must not leak either
        raise psycopg.OperationalError(
            f'connection to server at "db.invalid" failed: password authentication failed '
            f'for user "{FAKE_USER}" ({url})'
        )

    monkeypatch.setattr(psycopg, "connect", failing_connect)
    database = FakeMongoDb()
    monkeypatch.setattr(pymongo, "MongoClient", lambda url, **kw: FakeMongoClient(database))
    monkeypatch.setattr(postgres.time, "sleep", lambda _s: None)
    assert db_check.main() == 1
    out = capsys.readouterr()
    shown = out.out + out.err + log_text(settings)
    assert "db.invalid" in out.out and "cluster.invalid" in out.out
    for leaked in (FAKE_PW, FAKE_USER, PG_URL, MONGO_URL):
        assert leaked not in shown


# --- TLS rule: refuse unencrypted connections to non-local hosts ---------------------------

PLAIN_PW = "fake-pass-456"


@pytest.mark.parametrize(
    ("hosts", "local"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("[::1]:5432", True),
        ("postgres", True),  # Docker service name
        ("mongo,mongo2", True),
        ("db.example.com", False),
        ("127.0.0.1,db.example.com", False),
        ("10.0.0.5", False),
    ],
)
def test_is_local(hosts, local):
    from market_intel.db.common import is_local

    assert is_local(hosts) is local


@pytest.mark.parametrize(
    ("query", "encrypted"),
    [
        ("", False),
        ("?sslmode=disable", False),
        ("?sslmode=allow", False),
        ("?sslmode=prefer", False),
        ("?sslmode=require", True),
        ("?sslmode=verify-ca", True),
        ("?application_name=x&sslmode=verify-full", True),
    ],
)
def test_postgres_url_tls(query, encrypted):
    assert postgres.url_tls(f"postgresql://u:p@db.example.com/x{query}") is encrypted


@pytest.mark.parametrize(
    ("url", "tls"),
    [
        ("mongodb+srv://u:p@c.example.com/", True),
        ("mongodb+srv://u:p@c.example.com/?retryWrites=true", True),
        ("mongodb+srv://u:p@c.example.com/?tls=false", False),
        ("mongodb+srv://u:p@c.example.com/?ssl=FALSE", False),
        ("mongodb://u:p@db.example.com:27017/", False),
        ("mongodb://u:p@db.example.com:27017/?tls=false", False),
        ("mongodb://u:p@db.example.com:27017/?tls=true", True),
        ("mongodb://u:p@db.example.com:27017/?SSL=true", True),
        ("http://not-mongo", None),
    ],
)
def test_mongo_url_tls(url, tls):
    assert mongo.url_tls(url) is tls


def test_mongo_tls_fallback_is_safe_when_attribute_missing():
    broken = type("Client", (), {"options": object()})()
    assert mongo.tls_enabled(broken) is None


def _never(_db):
    pytest.fail("a connection was attempted")


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{FAKE_USER}:{PLAIN_PW}@db.example.com:5432/x",
        f"postgresql://{FAKE_USER}:{PLAIN_PW}@db.example.com:5432/x?sslmode=prefer",
    ],
)
def test_postgres_refuses_non_local_without_tls(tmp_path, monkeypatch, url):
    db = make_settings(tmp_path, monkeypatch, POSTGRES_URL=url).require("database")
    report = postgres.check(db, connector=_never)
    assert report.refused and not report.ok and report.tls is False
    assert "refusing unencrypted connection" in report.error
    assert PLAIN_PW not in report.error


def test_postgres_allows_local_without_tls(tmp_path, monkeypatch):
    url = f"postgresql://{FAKE_USER}:{PLAIN_PW}@127.0.0.1:5432/x"
    db = make_settings(tmp_path, monkeypatch, POSTGRES_URL=url).require("database")
    report = postgres.check(db, connector=lambda _db: FakePgConn(ssl=False))
    assert report.ok and not report.refused and report.tls is False


def test_mongo_refuses_non_local_without_tls(tmp_path, monkeypatch):
    url = f"mongodb://{FAKE_USER}:{PLAIN_PW}@db.example.com:27017/?tls=false"
    db = make_settings(tmp_path, monkeypatch, MONGODB_URI=url).require("database")
    report = mongo.check(db, client_factory=_never)
    assert report.refused and not report.ok and report.tls is False
    assert "refusing unencrypted connection" in report.error


def test_mongo_allows_docker_service_without_tls(tmp_path, monkeypatch):
    url = f"mongodb://{FAKE_USER}:{PLAIN_PW}@mongo:27017/?authSource=market_intel"
    db = make_settings(tmp_path, monkeypatch, MONGODB_URI=url).require("database")
    database = FakeMongoDb()
    report = mongo.check(db, client_factory=lambda _db: FakeMongoClient(database, tls=False))
    assert report.ok and report.tls is False  # read from the URL, not the private attribute


def test_mongo_tls_reported_from_url(tmp_path, monkeypatch):
    db = make_settings(tmp_path, monkeypatch).require("database")  # mongodb+srv://
    database = FakeMongoDb()
    report = mongo.check(db, client_factory=lambda _db: FakeMongoClient(database, tls=False))
    assert report.tls is True


def test_refusal_end_to_end_no_connection_and_no_password(tmp_path, monkeypatch, capsys):
    url = f"mongodb://{FAKE_USER}:{PLAIN_PW}@db.example.com:27017/?tls=false"
    settings = make_settings(tmp_path, monkeypatch, MONGODB_URI=url)
    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **kw: pytest.fail("connected"))
    checkers = {"postgres": lambda _db: ok_report("postgres"), "mongodb": mongo.check}
    assert db_check.run(settings, checkers=checkers) == 1
    out = capsys.readouterr().out
    assert "reachable : not tried (refused)" in out
    assert "refusing unencrypted connection" in out
    assert "alert     : critical -> dry_run" in out
    logs = log_text(settings)
    assert "Database connection not encrypted: mongodb" in logs
    assert PLAIN_PW not in out + logs and FAKE_USER not in out + logs
