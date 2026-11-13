"""Shared test guards: no real email or database, no real .env, clean logging."""

import smtplib
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pymongo
import pymongo.mongo_client
import pytest
import structlog

from market_intel import config
from market_intel.config import Settings, get_settings
from market_intel.logging_setup import reset_logging

SETTING_VARS = [name.upper() for name in Settings.model_fields] + [config.ENV_FILE_VAR]


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any real SMTP connection fails the test. Tests that need SMTP patch in a fake."""
    attempts: list[tuple[object, ...]] = []

    class Blocked:
        def __init__(self, *args: object, **kwargs: object) -> None:
            attempts.append(args)
            # pytest.fail raises a BaseException, so the alerts code can't swallow it
            pytest.fail("a test tried to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", Blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Blocked)
    yield
    assert not attempts, "real SMTP connection attempted"


@pytest.fixture(autouse=True)
def no_real_databases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any real psycopg or MongoClient connection fails the test. Tests patch in fakes."""
    attempts: list[str] = []

    def blocked(kind: str):
        def fail(*args: object, **kwargs: object) -> None:
            attempts.append(kind)
            pytest.fail(f"a test tried to open a real {kind} connection")

        return fail

    monkeypatch.setattr(psycopg, "connect", blocked("psycopg"))
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(blocked("psycopg")))
    monkeypatch.setattr(pymongo, "MongoClient", blocked("MongoClient"))
    monkeypatch.setattr(pymongo.mongo_client, "MongoClient", blocked("MongoClient"))
    yield
    assert not attempts, f"real database connection attempted: {attempts}"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear settings env vars and point MARKET_INTEL_ENV_FILE at an empty tmp file."""
    for name in SETTING_VARS:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(empty))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_logging()  # close file handlers so tmp dirs can be removed on Windows
    structlog.contextvars.clear_contextvars()
