"""Instruments refresh support: seen dates, symbol reuse, symbol history, watchlist.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22

- instruments.first_seen_on / last_seen_on: IST dates of the first and latest refresh that
  listed the instrument.
- UNIQUE (exchange, segment, symbol) becomes a unique index WHERE is_active, so a symbol can be
  reused after the old instrument goes inactive.
- instrument_symbol_history: one row per symbol change.
- watchlist: the owner's list (max 50, enforced by the app), one row per instrument.

mi_app gets its rights on the two new tables (and their identity sequence) from the default
privileges set in 0002; the integration tests check this by acting as mi_app.
Never run downgrade on real data (only in CI). Downgrade drops the history and the watchlist,
and restoring the plain UNIQUE constraint fails if an old symbol has been reused.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("first_seen_on", sa.Date(), nullable=True))
    op.add_column("instruments", sa.Column("last_seen_on", sa.Date(), nullable=True))
    op.drop_constraint(
        op.f("uq_instruments_exchange_segment_symbol"), "instruments", type_="unique"
    )
    op.create_index(
        "uq_instruments_active_symbol",
        "instruments",
        ["exchange", "segment", "symbol"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "instrument_symbol_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("old_symbol", sa.Text(), nullable=False),
        sa.Column("new_symbol", sa.Text(), nullable=False),
        sa.Column("changed_on", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_instrument_symbol_history_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_symbol_history")),
    )
    op.create_index(
        op.f("ix_instrument_symbol_history_instrument_id"),
        "instrument_symbol_history",
        ["instrument_id"],
        unique=False,
    )
    op.create_table(
        "watchlist",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("length(note) <= 200", name=op.f("ck_watchlist_note_length")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_watchlist_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_watchlist")),
    )


def downgrade() -> None:
    op.drop_table("watchlist")
    op.drop_index(
        op.f("ix_instrument_symbol_history_instrument_id"), table_name="instrument_symbol_history"
    )
    op.drop_table("instrument_symbol_history")
    op.drop_index("uq_instruments_active_symbol", table_name="instruments")
    op.create_unique_constraint(
        op.f("uq_instruments_exchange_segment_symbol"),
        "instruments",
        ["exchange", "segment", "symbol"],
    )
    op.drop_column("instruments", "last_seen_on")
    op.drop_column("instruments", "first_seen_on")
