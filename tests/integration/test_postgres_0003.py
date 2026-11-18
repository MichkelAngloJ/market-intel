"""Migration 0003 as the app login: updated_at is set by the trigger, and mi_app has an
explicit TEMP right. Runs in CI (RUN_INTEGRATION=1)."""

from collections.abc import Iterator

import psycopg
import pytest

from market_intel.config import get_settings
from market_intel.db import postgres

pytestmark = pytest.mark.integration


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as conn:
        yield conn


def test_trigger_updates_updated_at(app_conn):
    row = app_conn.execute(
        "INSERT INTO instruments (segment, symbol, name, updated_at)"
        " VALUES ('EQ', 'DEMO_0003', 'demo', now() - interval '1 day')"
        " RETURNING id, updated_at"
    ).fetchone()
    try:
        # The app doesn't set updated_at; the trigger does.
        app_conn.execute("UPDATE instruments SET name = 'demo2' WHERE id = %s", (row[0],))
        after = app_conn.execute(
            "SELECT updated_at FROM instruments WHERE id = %s", (row[0],)
        ).fetchone()[0]
        assert after > row[1]
    finally:
        app_conn.execute("DELETE FROM instruments WHERE id = %s", (row[0],))


def test_mi_app_has_explicit_temp_right(app_conn):
    acl = app_conn.execute(
        "SELECT datacl::text FROM pg_database WHERE datname = current_database()"
    ).fetchone()[0]
    assert "mi_app=T" in acl
    app_conn.execute("CREATE TEMP TABLE demo_temp (id int)")  # works
