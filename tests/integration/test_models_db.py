"""S3 as the app login: migration 0008 (model_versions) and a tiny training run registered as
candidates (synthetic data; MLflow in a temporary folder). One transaction, rolled back, so it is
safe on a real local database. Runs in CI compose-smoke (RUN_INTEGRATION=1)."""

from collections.abc import Iterator

import psycopg
import pytest
from model_fakes import dataset

from market_intel import models as M
from market_intel.config import get_settings
from market_intel.db import postgres

pytestmark = pytest.mark.integration


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with postgres.connect(get_settings().require("database")) as c:
        with c.transaction(force_rollback=True):
            c.execute("DELETE FROM model_versions WHERE name LIKE 'lgbm_21d_fake%%'")
            yield c


def test_0008_as_the_app_login(conn):
    assert conn.execute("SELECT current_user").fetchone()[0] == "mi_app"
    for right in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'model_versions', %s)", (right,)
        ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO model_versions (name, version, horizon, kind, train_from, train_to,"
            " metrics, beats_baseline, status) VALUES ('zz', 1, 21, 'pooled', '2024-01-01',"
            " '2024-06-01', '{}', false, 'promoted')"
        )


def test_a_tiny_train_registers_candidate_rows(conn, tmp_path):
    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    result = M.run_training(dataset(), min_sector_rows=500, min_sector_stocks=4)
    rows = M.register(conn, settings, result)
    names = [r["name"] for r in rows]
    stored = conn.execute(
        "SELECT name, version, kind, status, beats_baseline, mlflow_run_id IS NOT NULL,"
        " metrics ? 'walk_forward' FROM model_versions WHERE name = ANY(%s) ORDER BY name",
        (names,),
    ).fetchall()
    assert len(stored) == len(rows) == 4
    assert {r[3] for r in stored} == {"candidate"} and all(r[5] and r[6] for r in stored)
    assert {r[2] for r in stored} == {"sector", "pooled"}
    again = M.register(conn, settings, result, use_mlflow=False)
    assert {r["version"] for r in again} == {stored[0][1] + 1}
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(
            "INSERT INTO model_versions (name, version, horizon, kind, train_from, train_to,"
            " metrics, beats_baseline) SELECT name, version, horizon, kind, train_from,"
            " train_to, metrics, beats_baseline FROM model_versions WHERE name = %s LIMIT 1",
            (names[0],),
        )
