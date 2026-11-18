"""The TLS refusal inside the connection functions, the profile-dependent local rule and the
database_admin settings group. Fakes only: no network."""

import psycopg
import pymongo
import pytest

from market_intel.config import ConfigError, load_settings
from market_intel.db import mongo, postgres
from market_intel.db.common import InsecureConnectionError, host_is_local

FAKE_PW = "fake-pass-789"
FAKE_USER = "fake_user"


def db_settings(monkeypatch, profile: str, *, pg: str = "", mongo_uri: str = ""):
    monkeypatch.setenv("APP_PROFILE", profile)
    monkeypatch.setenv(
        "POSTGRES_URL", pg or f"postgresql://{FAKE_USER}:{FAKE_PW}@db.invalid/x?sslmode=require"
    )
    monkeypatch.setenv(
        "MONGODB_URI", mongo_uri or f"mongodb+srv://{FAKE_USER}:{FAKE_PW}@c.invalid/"
    )
    return load_settings().require("database")


@pytest.fixture
def pg_calls(monkeypatch):
    """Records psycopg.connect calls instead of connecting."""
    calls: list[dict] = []

    def fake_connect(url, **kwargs):
        calls.append(kwargs)
        return "conn"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return calls


@pytest.fixture
def mongo_calls(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(pymongo, "MongoClient", lambda url, **kw: calls.append(kw) or "client")
    return calls


# --- local rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hosts", "profile", "local"),
    [
        ("localhost", "restricted", True),
        ("127.0.0.1", "restricted", True),
        ("[::1]:5432", "restricted", True),
        ("postgres", "restricted", False),  # one word: could be any machine on the network
        ("postgres", "full", True),  # Docker service name
        ("mongo", "server", True),
        ("db.example.com", "full", False),
        ("localhost,mongo2", "restricted", False),
    ],
)
def test_host_is_local_depends_on_profile(hosts, profile, local):
    assert host_is_local(hosts, profile) is local


# --- postgres connect() ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "url"),
    [
        ("full", f"postgresql://{FAKE_USER}:{FAKE_PW}@db.example.com:5432/x?sslmode=disable"),
        ("full", f"postgresql://{FAKE_USER}:{FAKE_PW}@db.example.com:5432/x"),
        ("restricted", f"postgresql://{FAKE_USER}:{FAKE_PW}@postgres:5432/x"),
    ],
)
def test_postgres_connect_refuses_without_connecting(monkeypatch, pg_calls, profile, url):
    db = db_settings(monkeypatch, profile, pg=url)
    with pytest.raises(InsecureConnectionError) as exc:
        postgres.connect(db)
    assert pg_calls == []  # no connection attempted
    assert "refusing unencrypted connection" in str(exc.value)
    assert FAKE_PW not in str(exc.value) and FAKE_USER not in str(exc.value)


@pytest.mark.parametrize(
    ("profile", "host"),
    [("restricted", "127.0.0.1"), ("restricted", "localhost"), ("full", "postgres")],
)
def test_postgres_connect_allows_local_without_tls(monkeypatch, pg_calls, profile, host):
    db = db_settings(monkeypatch, profile, pg=f"postgresql://{FAKE_USER}:{FAKE_PW}@{host}/x")
    assert postgres.connect(db) == "conn"
    assert len(pg_calls) == 1


def test_postgres_connect_url_passes_autocommit(pg_calls):
    url = f"postgresql://{FAKE_USER}:{FAKE_PW}@db.invalid/x?sslmode=verify-full"
    postgres.connect_url(url, profile="restricted", timeout_s=5, autocommit=False)
    assert pg_calls == [
        {"connect_timeout": 5, "application_name": "market-intel", "autocommit": False}
    ]


def test_postgres_check_refuses_service_name_on_restricted(monkeypatch):
    db = db_settings(monkeypatch, "restricted", pg=f"postgresql://{FAKE_USER}:{FAKE_PW}@postgres/x")
    report = postgres.check(db, connector=lambda _db: pytest.fail("connected"))
    assert report.refused and report.tls is False


# --- mongo client() --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "url"),
    [
        ("full", f"mongodb://{FAKE_USER}:{FAKE_PW}@db.example.com:27017/?tls=false"),
        ("full", f"mongodb+srv://{FAKE_USER}:{FAKE_PW}@c.example.com/?ssl=false"),
        ("restricted", f"mongodb://{FAKE_USER}:{FAKE_PW}@mongo:27017/"),
    ],
)
def test_mongo_client_refuses_without_creating_client(monkeypatch, mongo_calls, profile, url):
    db = db_settings(monkeypatch, profile, mongo_uri=url)
    with pytest.raises(InsecureConnectionError) as exc:
        mongo.client(db)
    assert mongo_calls == []  # no client created, so nothing was sent
    assert FAKE_PW not in str(exc.value) and FAKE_USER not in str(exc.value)


@pytest.mark.parametrize(
    ("profile", "host"), [("restricted", "127.0.0.1:27017"), ("full", "mongo:27017")]
)
def test_mongo_client_allows_local_without_tls(monkeypatch, mongo_calls, profile, host):
    db = db_settings(monkeypatch, profile, mongo_uri=f"mongodb://{FAKE_USER}:{FAKE_PW}@{host}/")
    assert mongo.client(db) == "client"
    assert len(mongo_calls) == 1


# --- database_admin group --------------------------------------------------------------------


def test_database_admin_group_missing_is_clear_error(monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    with pytest.raises(ConfigError, match="POSTGRES_ADMIN_URL"):
        load_settings().require("database_admin")


def test_database_admin_group_hides_the_url(monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "restricted")
    url = f"postgresql://owner:{FAKE_PW}@db.invalid/x?sslmode=require"
    monkeypatch.setenv("POSTGRES_ADMIN_URL", url)
    admin = load_settings().require("database_admin")
    assert admin.postgres_admin_url.get_secret_value() == url
    assert admin.app_profile == "restricted"
    assert FAKE_PW not in repr(admin) and FAKE_PW not in str(admin)
