"""Tests for the ingestion-run helper. Fakes only: no network (see tests/integration for the
real app login)."""

import io
import json
from pathlib import Path

import pytest

from market_intel.config import ConfigError, Settings, load_settings
from market_intel.ingestion import ERROR_MAX_CHARS, ingestion_run, masked_error
from market_intel.logging_setup import configure_logging

FAKE_PW = "fake-pass-456"
FAKE_USER = "fake_user"
PG_URL = f"postgresql://{FAKE_USER}:{FAKE_PW}@db.invalid:5432/x?sslmode=require"
MONGO_URL = f"mongodb+srv://{FAKE_USER}:{FAKE_PW}@cluster.invalid/"


def make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    base = {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": PG_URL,
        "MONGODB_URI": MONGO_URL,
        "ALERT_DRY_RUN": "true",
    }
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    configure_logging(settings, "ingestion_test", console_stream=io.StringIO())
    return settings


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    """Keeps ingestion_runs rows in a dict and understands the helper's three statements."""

    def __init__(self, fail_updates: bool = False):
        self.rows: dict[int, dict] = {}
        self.fail_updates = fail_updates
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def execute(self, sql: str, params: tuple):
        if sql.startswith("INSERT INTO ingestion_runs"):
            job, source, params_json = params
            run_id = len(self.rows) + 1
            self.rows[run_id] = {
                "job": job,
                "source": source,
                "params": json.loads(params_json) if params_json else None,
                "status": "running",
                "finished": False,
                "rows_written": 0,
                "error": None,
            }
            return FakeResult((run_id,))
        if sql.startswith("UPDATE ingestion_runs"):
            if self.fail_updates:
                raise RuntimeError("database went away")
            row_count, *rest = params
            row = self.rows[rest[-1]]
            row.update(rows_written=row_count, finished=True)
            if "'failed'" in sql:
                row.update(status="failed", error=rest[0])
            else:
                row["status"] = "success"
            return FakeResult(None)
        raise AssertionError(f"unexpected SQL: {sql}")


def connector(conn: FakeConn):
    def connect(db):
        assert db.postgres_url.get_secret_value() == PG_URL  # the APP login
        return conn

    return connect


def test_success_records_running_then_success(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn()
    with ingestion_run(
        settings, "daily_candles", source="fake", params={"days": 5}, connect=connector(conn)
    ) as run:
        assert conn.rows[run.id]["status"] == "running"  # visible while the job runs
        run.add_rows(3)
        run.add_rows(4)
    row = conn.rows[run.id]
    assert row == {
        "job": "daily_candles",
        "source": "fake",
        "params": {"days": 5},
        "status": "success",
        "finished": True,
        "rows_written": 7,
        "error": None,
    }
    assert conn.closed


def test_failure_records_masked_error_and_reraises(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn()
    boom = RuntimeError(f"could not connect to {PG_URL} with password {FAKE_PW}")
    with pytest.raises(RuntimeError) as caught:
        with ingestion_run(settings, "news", source="fake", connect=connector(conn)) as run:
            run.add_rows(2)
            raise boom
    assert caught.value is boom  # the job's exception, unchanged
    row = conn.rows[run.id]
    assert row["status"] == "failed" and row["finished"] and row["rows_written"] == 2
    assert row["error"].startswith("RuntimeError: could not connect")
    assert FAKE_PW not in row["error"] and FAKE_USER not in row["error"]


def test_ctrl_c_also_marks_the_run_failed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn()
    with pytest.raises(KeyboardInterrupt):
        with ingestion_run(settings, "news", connect=connector(conn)) as run:
            raise KeyboardInterrupt
    assert conn.rows[run.id]["status"] == "failed"
    assert conn.rows[run.id]["error"].startswith("KeyboardInterrupt")


def test_a_failed_status_update_never_hides_the_jobs_error(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn(fail_updates=True)
    with pytest.raises(ValueError, match="job error"):
        with ingestion_run(settings, "news", connect=connector(conn)):
            raise ValueError("job error")


def test_secret_looking_params_are_masked(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn()
    with ingestion_run(
        settings, "news", params={"feed": "et", "token": "abc123"}, connect=connector(conn)
    ) as run:
        pass
    assert conn.rows[run.id]["params"]["feed"] == "et"
    assert "abc123" not in json.dumps(conn.rows[run.id]["params"])


def test_negative_row_counts_are_refused(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    conn = FakeConn()
    with pytest.raises(ValueError):
        with ingestion_run(settings, "news", connect=connector(conn)) as run:
            run.add_rows(-1)
    assert conn.rows[run.id]["status"] == "failed"


def test_missing_database_settings_fail_before_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = load_settings()

    def never(db):
        pytest.fail("connected without database settings")

    with pytest.raises(ConfigError, match="POSTGRES_URL"):
        with ingestion_run(settings, "news", connect=never):
            pass


def test_job_name_is_required(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        with ingestion_run(settings, "", connect=connector(FakeConn())):
            pass


def test_masked_error_is_cut_to_a_safe_length():
    text = masked_error(RuntimeError("x" * 10_000))
    assert len(text) == ERROR_MAX_CHARS and text.endswith("...")


def test_masked_error_hides_user_names_in_any_url():
    other = "mongodb://" + "alice" + ":" + "s3cr3t-value" + "@cluster.invalid/db"
    text = masked_error(RuntimeError(f"refused {other} and https://bob@example.invalid/x"))
    assert "alice" not in text and "s3cr3t-value" not in text and "bob" not in text
    assert "cluster.invalid" in text  # hosts may stay


def test_masked_error_removes_the_database_url_without_logging_configured():
    text = masked_error(RuntimeError(f"failed: {PG_URL}"), PG_URL)
    assert FAKE_PW not in text and FAKE_USER not in text
