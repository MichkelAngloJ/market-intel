"""Grants for the limited app login mi_app: read and write rows, nothing else.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18

- Creates the role mi_app (NOLOGIN) only if it does not exist. An existing role is never
  changed: its LOGIN and password are set by hand on Neon and by an init script in Docker.
  No password ever appears in a migration.
- mi_app gets SELECT/INSERT/UPDATE/DELETE on tables and USAGE/SELECT on sequences (needed for
  identity columns). No CREATE, TRUNCATE, REFERENCES, TRIGGER or ALTER; DROP and ALTER need
  ownership, which stays with the migrating (owner) role.
- Default privileges give tables and sequences that later migrations create (as the same
  owner role) the same grants.
- alembic_version is read-only for mi_app.
- Downgrade revokes the grants but does NOT drop the role.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "mi_app"

CREATE_ROLE_IF_MISSING = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} NOLOGIN;
    END IF;
END
$$
"""

TABLE_RIGHTS = "SELECT, INSERT, UPDATE, DELETE"
SEQUENCE_RIGHTS = "USAGE, SELECT"


def upgrade() -> None:
    op.execute(CREATE_ROLE_IF_MISSING)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT {TABLE_RIGHTS} ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT {SEQUENCE_RIGHTS} ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Without FOR ROLE this applies to objects created later by the current (migrating) role.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {TABLE_RIGHTS} ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT {SEQUENCE_RIGHTS} ON SEQUENCES TO {APP_ROLE}"
    )
    # The app may read the schema version but never change it.
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT ON TABLE alembic_version TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE {SEQUENCE_RIGHTS} ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE {TABLE_RIGHTS} ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE {SEQUENCE_RIGHTS} ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE {TABLE_RIGHTS} ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    # The role itself stays: on Neon it was created by hand and holds the app's login.
