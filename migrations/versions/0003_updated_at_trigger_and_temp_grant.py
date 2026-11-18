"""updated_at trigger on instruments, and an explicit TEMP right for mi_app.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18

- set_updated_at() + a BEFORE UPDATE trigger keep instruments.updated_at current, so callers
  can't forget it. now() is the transaction's start time, the same for every row it changes.
- GRANT TEMP on the current database to mi_app, so db_check's write test (a TEMP table) no
  longer depends on Postgres' default grant to PUBLIC. The database name differs between
  Neon (neondb) and Docker (market_intel), so a DO block reads it with current_database()
  and quotes it with quote_ident() (same quoting as format('%I'), without a percent sign).
Never run downgrade on real data (only in CI).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "mi_app"

CREATE_FUNCTION = """
CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$
"""

CREATE_TRIGGER = """
CREATE TRIGGER trg_instruments_set_updated_at
BEFORE UPDATE ON instruments
FOR EACH ROW EXECUTE FUNCTION set_updated_at()
"""


def _temp_grant(verb: str, preposition: str) -> str:
    # quote_ident() quotes the database name safely, like format('%I'). No '%' here on
    # purpose: SQLAlchemy escapes '%' for the psycopg driver, which would make this fragile.
    return f"""
DO $$
BEGIN
    EXECUTE '{verb} TEMP ON DATABASE ' || quote_ident(current_database())
        || ' {preposition} {APP_ROLE}';
END
$$
"""


def upgrade() -> None:
    op.execute(CREATE_FUNCTION)
    op.execute(CREATE_TRIGGER)
    op.execute(_temp_grant("GRANT", "TO"))


def downgrade() -> None:
    op.execute(_temp_grant("REVOKE", "FROM"))
    op.execute("DROP TRIGGER trg_instruments_set_updated_at ON instruments")
    op.execute("DROP FUNCTION set_updated_at()")
