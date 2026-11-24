"""Model registry (sprint S3).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24

- model_versions: one row per trained model version (name, version, horizon, sector, kind
  sector|pooled, training range, holdout start, metrics, beats_baseline, status
  candidate|active|retired with default candidate, MLflow run id, model card path).
  UNIQUE (name, version). Nothing becomes "active" without the owner.

mi_app gets its rights from the default privileges set in 0002 (table and its identity
sequence). Never run downgrade on real data (only in CI): it drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("sector", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "trained_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("train_from", sa.Date(), nullable=False),
        sa.Column("train_to", sa.Date(), nullable=False),
        sa.Column("holdout_from", sa.Date(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("beats_baseline", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), server_default="candidate", nullable=False),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column("card_path", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'retired')", name=op.f("ck_model_versions_status")
        ),
        sa.CheckConstraint("kind IN ('sector', 'pooled')", name=op.f("ck_model_versions_kind")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_model_versions_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_versions")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_model_versions_name_version")),
    )


def downgrade() -> None:
    op.drop_table("model_versions")
