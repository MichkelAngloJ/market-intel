"""The ingestion-run helper works through the limited app login (POSTGRES_URL = mi_app), by
behaviour: it inserts a running row, then marks it success or failed with a masked error.

Runs in CI compose-smoke after "alembic upgrade head" (RUN_INTEGRATION=1).
"""

from collections.abc import Iterator

import psycopg
import pytest

from market_intel.config import get_settings
from market_intel.db import postgres
from market_intel.ingestion import ingestion_run

pytestmark = pytest.mark.integration

JOB = "zz_integration_test_ingestion"
# Built from parts so the secret scanner doesn't read it as a real credential.
FAKE_USER, FAKE_PW = "someone", "not-a-real-password-789"
FAKE_URL = "postgresql://" + FAKE_USER + ":" + FAKE_PW + "@db.invalid/x"


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as conn:
        conn.execute("DELETE FROM ingestion_runs WHERE job = %s", (JOB,))
        yield conn
        conn.execute("DELETE FROM ingestion_runs WHERE job = %s", (JOB,))


def fetch(conn: psycopg.Connection, run_id: int):
    return conn.execute(
        "SELECT job, source, status, rows_written, params, error, finished_at IS NOT NULL"
        " FROM ingestion_runs WHERE id = %s",
        (run_id,),
    ).fetchone()


def test_success_run_through_the_app_login(app_conn):
    assert app_conn.execute("SELECT current_user").fetchone()[0] == "mi_app"
    settings = get_settings()
    with ingestion_run(settings, JOB, source="fake", params={"days": 3}) as run:
        # Committed at once on the helper's own connection: another session sees it.
        assert fetch(app_conn, run.id)[2] == "running"
        run.add_rows(5)
    assert fetch(app_conn, run.id) == (JOB, "fake", "success", 5, {"days": 3}, None, True)


def test_failed_run_holds_only_a_masked_error(app_conn):
    settings = get_settings()
    with pytest.raises(RuntimeError):
        with ingestion_run(settings, JOB, source="fake") as run:
            run.add_rows(1)
            raise RuntimeError(f"upstream refused {FAKE_URL}")
    job, source, status, rows, _params, error, finished = fetch(app_conn, run.id)
    assert (status, rows, finished) == ("failed", 1, True)
    assert error.startswith("RuntimeError: upstream refused")
    assert FAKE_PW not in error and FAKE_USER not in error
