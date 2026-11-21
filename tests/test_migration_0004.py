"""Migration 0004 without a database: offline SQL, a working downgrade, and the schema file it
must match (CI's compose-smoke runs up/down/up and alembic check for real)."""

import io
from pathlib import Path

from alembic import command
from alembic.config import Config

from market_intel.db.schema import WATCHLIST_NOTE_MAX, instruments, metadata

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "migrations" / "versions" / "0004_instruments_history_watchlist.py"


def upgrade_sql() -> str:
    buffer = io.StringIO()
    cfg = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0003:0004", sql=True)
    return buffer.getvalue()


def downgrade_sql() -> str:
    buffer = io.StringIO()
    cfg = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=buffer)
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "0004:0003", sql=True)
    return buffer.getvalue()


def test_upgrade_adds_seen_dates_partial_unique_index_and_new_tables():
    text = upgrade_sql()
    assert "ALTER TABLE instruments ADD COLUMN first_seen_on DATE" in text
    assert "ALTER TABLE instruments ADD COLUMN last_seen_on DATE" in text
    assert "DROP CONSTRAINT uq_instruments_exchange_segment_symbol" in text
    assert (
        "CREATE UNIQUE INDEX uq_instruments_active_symbol ON instruments "
        "(exchange, segment, symbol) WHERE is_active"
    ) in text
    assert "CREATE TABLE instrument_symbol_history (" in text
    assert "CREATE TABLE watchlist (" in text
    assert f"CHECK (length(note) <= {WATCHLIST_NOTE_MAX})" in text
    assert "UPDATE alembic_version SET version_num='0004'" in text


def test_upgrade_never_touches_roles_or_rights():
    # mi_app's rights on the new tables come from 0002's default privileges.
    text = upgrade_sql().upper()
    for forbidden in (
        "GRANT",
        "REVOKE",
        "ROLE",
        "PASSWORD",
        "DROP TABLE",
        "DELETE FROM",
        "TRUNCATE",
    ):
        assert forbidden not in text


def test_downgrade_undoes_everything_in_reverse():
    text = downgrade_sql()
    order = [
        "DROP TABLE watchlist",
        "DROP TABLE instrument_symbol_history",
        "DROP INDEX uq_instruments_active_symbol",
        "ADD CONSTRAINT uq_instruments_exchange_segment_symbol UNIQUE (exchange, segment, symbol)",
        "DROP COLUMN last_seen_on",
        "DROP COLUMN first_seen_on",
        "UPDATE alembic_version SET version_num='0003'",
    ]
    positions = [text.index(part) for part in order]
    assert positions == sorted(positions)


def test_schema_matches_the_migration():
    assert {"instrument_symbol_history", "watchlist"} <= set(metadata.tables)
    assert {"first_seen_on", "last_seen_on"} <= set(instruments.c.keys())
    [index] = [i for i in instruments.indexes if i.name == "uq_instruments_active_symbol"]
    assert index.unique and str(index.dialect_options["postgresql"]["where"]) == "is_active"
    assert not [
        c for c in instruments.constraints if c.name == "uq_instruments_exchange_segment_symbol"
    ]
    assert "Never run downgrade on real data" in MIGRATION.read_text(encoding="utf-8")
