"""BSE scrip codes on instruments.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22

- instruments.bse_code: the BSE scrip code, filled by the instruments refresh from Upstox's
  public BSE file (matched by ISIN), so BSE announcements link to stocks exactly.
- A unique index on bse_code WHERE bse_code IS NOT NULL: one stock per code; rows without a
  code are not compared.

Never run downgrade on real data (only in CI): it drops the column and its codes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("bse_code", sa.Text(), nullable=True))
    op.create_index(
        "uq_instruments_bse_code",
        "instruments",
        ["bse_code"],
        unique=True,
        postgresql_where=sa.text("bse_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_instruments_bse_code", table_name="instruments")
    op.drop_column("instruments", "bse_code")
