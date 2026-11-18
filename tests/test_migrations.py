"""Migrations without a database: the offline SQL, the safety checks in env.py and the schema
rules. The real apply / undo / re-apply runs in CI (compose-smoke) against Docker Postgres."""

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from market_intel.db.schema import V_LLM_SPEND_MONTHLY, metadata

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_PW = "fake-pass-321"


def alembic_config(buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.attributes["configure_logger"] = False  # leave the test logging setup alone
    return cfg


def offline_sql(target: str = "head") -> str:
    buffer = io.StringIO()
    command.upgrade(alembic_config(buffer), target, sql=True)
    return buffer.getvalue()


def test_alembic_ini_has_no_database_url():
    text = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert not re.search(r"^\s*sqlalchemy\.url\s*=", text, re.MULTILINE)
    assert not re.search(r"(postgres|postgresql|mongodb)(\+\w+)?://", text)


def test_offline_sql_creates_schema_view_and_grants():
    sql = offline_sql()
    for table in metadata.tables:
        assert f"CREATE TABLE {table} (" in sql
    assert f"CREATE VIEW {V_LLM_SPEND_MONTHLY} AS" in sql
    assert "AT TIME ZONE 'Asia/Kolkata'" in sql
    assert "USING brin (trade_date)" in sql and "USING brin (ts)" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mi_app" in sql
    assert "GRANT SELECT ON TABLE alembic_version TO mi_app" in sql


def test_migrations_never_set_a_password_or_login():
    sql = offline_sql().upper()
    assert "PASSWORD" not in sql
    assert "ALTER ROLE" not in sql
    assert "CREATE ROLE MI_APP NOLOGIN" in sql
    for forbidden in ("TRUNCATE", "REFERENCES ON", "TRIGGER ON", "GRANT CREATE", "GRANT ALL"):
        assert forbidden not in sql


def test_downgrade_keeps_the_role():
    versions = REPO_ROOT / "migrations" / "versions"
    downgrade = (versions / "0002_app_role_grants.py").read_text(encoding="utf-8")
    downgrade = downgrade.split("def downgrade", 1)[1]
    assert "DROP ROLE" not in downgrade.upper()


def test_every_constraint_and_index_is_named():
    for table in metadata.tables.values():
        for constraint in table.constraints:
            assert constraint.name, f"unnamed constraint on {table.name}"
        for index in table.indexes:
            assert index.name, f"unnamed index on {table.name}"


def test_env_refuses_missing_admin_url(monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "hp")
    with pytest.raises(SystemExit, match="POSTGRES_ADMIN_URL"):
        command.current(alembic_config())


def test_env_refuses_unencrypted_non_local_admin_url(monkeypatch, capsys):
    monkeypatch.setenv("APP_PROFILE", "thinkpad")
    monkeypatch.setenv(
        "POSTGRES_ADMIN_URL", f"postgresql://owner:{FAKE_PW}@db.example.com/x?sslmode=disable"
    )
    # The autouse guard in conftest fails the test if a connection is attempted.
    with pytest.raises(SystemExit) as exc:
        command.upgrade(alembic_config(), "head")
    assert "refusing unencrypted connection" in str(exc.value)
    assert FAKE_PW not in str(exc.value)
    out = capsys.readouterr()
    assert FAKE_PW not in out.out + out.err


# --- 0003: updated_at trigger and TEMP grant --------------------------------------------------


def test_0003_trigger_and_temp_grant_in_offline_sql():
    sql = offline_sql()
    assert "CREATE FUNCTION set_updated_at() RETURNS trigger" in sql
    assert "BEFORE UPDATE ON instruments" in sql
    assert "'GRANT TEMP ON DATABASE ' || quote_ident(current_database())" in sql
    assert "|| ' TO mi_app'" in sql
    assert "UPDATE alembic_version SET version_num='0003'" in sql


def test_0003_is_head_and_downgrade_keeps_role():
    versions = REPO_ROOT / "migrations" / "versions"
    text = (versions / "0003_updated_at_trigger_and_temp_grant.py").read_text(encoding="utf-8")
    downgrade = text.split("def downgrade", 1)[1].upper()
    assert "REVOKE" in downgrade and "DROP ROLE" not in downgrade
