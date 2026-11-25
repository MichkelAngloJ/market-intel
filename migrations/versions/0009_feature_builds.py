"""Builds of file-based datasets (sprint S4i).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25

- feature_builds: one row per build of the intraday features or labels (Parquet files under
  DATA_DIR, never committed): kind, version, date range, rows, files and each file's SHA-256.

mi_app gets its rights from the default privileges set in 0002 (table and identity sequence).
Never run downgrade on real data (only in CI): it drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_builds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("sha256", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "built_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('intraday_features', 'intraday_labels')", name=op.f("ck_feature_builds_kind")
        ),
        sa.CheckConstraint(
            "rows >= 0 AND files >= 0", name=op.f("ck_feature_builds_counts_non_negative")
        ),
        sa.CheckConstraint("to_date >= from_date", name=op.f("ck_feature_builds_date_order")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_builds")),
    )


def downgrade() -> None:
    op.drop_table("feature_builds")
