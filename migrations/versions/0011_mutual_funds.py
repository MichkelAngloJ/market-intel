"""Mutual funds: schemes, daily NAV and the download chunks (sprint S6a).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-26

- mf_schemes: one row per AMFI scheme code (a code is a plan AND an option, so Direct Growth
  and Regular IDCW of the same fund are separate rows), with AMFI's own category heading kept
  verbatim beside the four groups the project reasons about.
- mf_nav: daily NAV, one row per scheme and date.
- mf_nav_chunks: one row per 90-day download (AMFI's limit per request), which is what makes
  a 15-year backfill resumable.

mi_app gets its rights from the default privileges set in 0002 (tables and identity
sequences). Never run downgrade on real data (only in CI): it drops all three tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mf_schemes",
        sa.Column("scheme_code", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("scheme_name", sa.Text(), nullable=False),
        sa.Column("amc", sa.Text(), nullable=False),
        sa.Column("amfi_category", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("scheme_type", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("option", sa.Text(), nullable=False),
        sa.Column("isin_growth", sa.Text(), nullable=True),
        sa.Column("isin_reinvest", sa.Text(), nullable=True),
        sa.Column("in_universe", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IN ('equity', 'hybrid', 'index', 'other')",
            name=op.f("ck_mf_schemes_category"),
        ),
        sa.CheckConstraint("last_seen >= first_seen", name=op.f("ck_mf_schemes_date_order")),
        sa.CheckConstraint(
            "option IN ('growth', 'idcw', 'unknown')", name=op.f("ck_mf_schemes_option")
        ),
        sa.CheckConstraint(
            "plan IN ('direct', 'regular', 'unknown')", name=op.f("ck_mf_schemes_plan")
        ),
        sa.CheckConstraint(
            "scheme_type IN ('open', 'close', 'interval')",
            name=op.f("ck_mf_schemes_scheme_type"),
        ),
        sa.PrimaryKeyConstraint("scheme_code", name=op.f("pk_mf_schemes")),
    )
    op.create_index(
        op.f("ix_mf_schemes_category"), "mf_schemes", ["category", "in_universe"], unique=False
    )
    op.create_table(
        "mf_nav",
        sa.Column("scheme_code", sa.Integer(), nullable=False),
        sa.Column("nav_date", sa.Date(), nullable=False),
        sa.Column("nav", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "ingested_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("nav > 0", name=op.f("ck_mf_nav_nav_positive")),
        sa.CheckConstraint("source IN ('amfi', 'mfapi')", name=op.f("ck_mf_nav_source")),
        sa.ForeignKeyConstraint(
            ["scheme_code"],
            ["mf_schemes.scheme_code"],
            name=op.f("fk_mf_nav_scheme_code_mf_schemes"),
        ),
        sa.PrimaryKeyConstraint("scheme_code", "nav_date", name=op.f("pk_mf_nav")),
    )
    op.create_index(op.f("ix_mf_nav_nav_date"), "mf_nav", ["nav_date"], unique=False)
    op.create_table(
        "mf_nav_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rows >= 0 AND bytes >= 0", name=op.f("ck_mf_nav_chunks_counts_non_negative")
        ),
        sa.CheckConstraint("to_date >= from_date", name=op.f("ck_mf_nav_chunks_date_order")),
        sa.CheckConstraint("source IN ('amfi', 'mfapi')", name=op.f("ck_mf_nav_chunks_source")),
        sa.CheckConstraint("status IN ('loaded', 'failed')", name=op.f("ck_mf_nav_chunks_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mf_nav_chunks")),
        sa.UniqueConstraint(
            "from_date", "to_date", "source", name=op.f("uq_mf_nav_chunks_from_date_to_date_source")
        ),
    )


def downgrade() -> None:
    op.drop_table("mf_nav_chunks")
    op.drop_index(op.f("ix_mf_nav_nav_date"), table_name="mf_nav")
    op.drop_table("mf_nav")
    op.drop_index(op.f("ix_mf_schemes_category"), table_name="mf_schemes")
    op.drop_table("mf_schemes")
