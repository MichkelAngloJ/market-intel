"""Universe membership and price events.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24

- universe_members (universe, instrument_id, as_of): e.g. "nifty500", written by the sector
  import. PK (universe, instrument_id).
- price_events (instrument_id, trade_date, kind, ratio, detected_at): suspected split/bonus
  jumps found in daily candles; prices are never changed. PK (instrument_id, trade_date, kind).

mi_app gets its rights from the default privileges set in 0002.
Never run downgrade on real data (only in CI): it drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "universe_members",
        sa.Column("universe", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_universe_members_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("universe", "instrument_id", name=op.f("pk_universe_members")),
    )
    op.create_table(
        "price_events",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ratio", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "detected_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('suspected_split_bonus')", name=op.f("ck_price_events_kind")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_price_events_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint(
            "instrument_id", "trade_date", "kind", name=op.f("pk_price_events")
        ),
    )


def downgrade() -> None:
    op.drop_table("price_events")
    op.drop_table("universe_members")
