"""The limited app login (POSTGRES_URL = mi_app) can read and write rows, and nothing else.

Runs in CI against Docker Postgres after "alembic upgrade head" (RUN_INTEGRATION=1).
"""

from collections.abc import Iterator

import psycopg
import pytest

from market_intel.config import get_settings
from market_intel.db import postgres

pytestmark = pytest.mark.integration

TEST_SYMBOL = "ZZ_INTEGRATION_TEST"


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as conn:
        yield conn


def test_connected_as_the_app_login(app_conn):
    user = app_conn.execute("SELECT current_user").fetchone()[0]
    assert user == "mi_app"
    flags = app_conn.execute(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles"
        " WHERE rolname = current_user"
    ).fetchone()
    assert flags == (False, False, False, False)


def test_app_can_insert_update_delete_instruments(app_conn):
    row = app_conn.execute(
        "INSERT INTO instruments (segment, symbol, name) VALUES ('EQ', %s, 'test') RETURNING id",
        (TEST_SYMBOL,),
    ).fetchone()
    try:
        app_conn.execute(
            "UPDATE instruments SET name = 'updated', updated_at = now() WHERE id = %s", row
        )
        name = app_conn.execute("SELECT name FROM instruments WHERE id = %s", row).fetchone()
        assert name == ("updated",)
    finally:
        app_conn.execute("DELETE FROM instruments WHERE id = %s", row)
    left = app_conn.execute(
        "SELECT count(*) FROM instruments WHERE symbol = %s", (TEST_SYMBOL,)
    ).fetchone()
    assert left == (0,)


def test_app_can_read_alembic_version_and_spend_view(app_conn):
    assert app_conn.execute("SELECT version_num FROM alembic_version").fetchone()
    app_conn.execute("SELECT * FROM v_llm_spend_monthly").fetchall()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE mi_app_probe (id int)",
        "DROP TABLE instruments",
        "TRUNCATE instruments",
        "ALTER TABLE instruments ADD COLUMN probe int",
        "CREATE INDEX mi_app_probe_ix ON instruments (symbol)",
        "INSERT INTO alembic_version (version_num) VALUES ('probe')",
        "UPDATE alembic_version SET version_num = version_num",
        "DELETE FROM alembic_version",
    ],
)
def test_app_is_refused(app_conn, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(sql)
