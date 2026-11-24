"""Features v1 and labels (sprint S2).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24

- features_daily: one row per (instrument, trade_date, feature_version) with a double column per
  v1 feature (the list is written out here, frozen; market_intel.feature_spec is the source).
- labels_daily: a SEPARATE table (features can never contain labels): forward return over
  21 / 63 / 126 regular trading days, up/down, or NULL with an exclusion reason.

mi_app gets its rights from the default privileges set in 0002.
Never run downgrade on real data (only in CI): it drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "features_daily",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ret_1d", sa.Float(precision=53), nullable=True),
        sa.Column("ret_5d", sa.Float(precision=53), nullable=True),
        sa.Column("ret_20d", sa.Float(precision=53), nullable=True),
        sa.Column("ret_60d", sa.Float(precision=53), nullable=True),
        sa.Column("ret_120d", sa.Float(precision=53), nullable=True),
        sa.Column("vol_20d", sa.Float(precision=53), nullable=True),
        sa.Column("vol_60d", sa.Float(precision=53), nullable=True),
        sa.Column("atr14_pct", sa.Float(precision=53), nullable=True),
        sa.Column("close_sma20", sa.Float(precision=53), nullable=True),
        sa.Column("close_sma50", sa.Float(precision=53), nullable=True),
        sa.Column("close_sma200", sa.Float(precision=53), nullable=True),
        sa.Column("sma20_sma50", sa.Float(precision=53), nullable=True),
        sa.Column("rsi14", sa.Float(precision=53), nullable=True),
        sa.Column("volume_z20", sa.Float(precision=53), nullable=True),
        sa.Column("log_traded_value_20d", sa.Float(precision=53), nullable=True),
        sa.Column("high_52w", sa.Float(precision=53), nullable=True),
        sa.Column("low_52w", sa.Float(precision=53), nullable=True),
        sa.Column("gap_open", sa.Float(precision=53), nullable=True),
        sa.Column("rs_nifty_20d", sa.Float(precision=53), nullable=True),
        sa.Column("rs_sector_20d", sa.Float(precision=53), nullable=True),
        sa.Column("mkt_ret_5d", sa.Float(precision=53), nullable=True),
        sa.Column("mkt_ret_20d", sa.Float(precision=53), nullable=True),
        sa.Column("mkt_close_sma200", sa.Float(precision=53), nullable=True),
        sa.Column("vix_level", sa.Float(precision=53), nullable=True),
        sa.Column("vix_chg_5d", sa.Float(precision=53), nullable=True),
        sa.Column("day_of_week", sa.Float(precision=53), nullable=True),
        sa.Column("month", sa.Float(precision=53), nullable=True),
        sa.Column("history_days", sa.Float(precision=53), nullable=True),
        sa.Column("price_event_5d", sa.Float(precision=53), nullable=True),
        sa.Column("news_exchange_1d", sa.Float(precision=53), nullable=True),
        sa.Column("news_exchange_5d", sa.Float(precision=53), nullable=True),
        sa.Column("news_company_5d", sa.Float(precision=53), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_features_daily_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint(
            "instrument_id", "trade_date", "feature_version", name=op.f("pk_features_daily")
        ),
    )
    op.create_table(
        "labels_daily",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("fwd_return", sa.Float(precision=53), nullable=True),
        sa.Column("up", sa.Boolean(), nullable=True),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column(
            "computed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("horizon_days IN (21, 63, 126)", name=op.f("ck_labels_daily_horizon")),
        sa.CheckConstraint(
            "excluded_reason IS NULL OR excluded_reason IN"
            " ('beyond_data', 'special_session', 'price_event', 'gap')",
            name=op.f("ck_labels_daily_reason"),
        ),
        sa.CheckConstraint(
            "(fwd_return IS NULL) = (excluded_reason IS NOT NULL)"
            " AND (up IS NULL) = (fwd_return IS NULL)",
            name=op.f("ck_labels_daily_label_or_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_labels_daily_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint(
            "instrument_id", "trade_date", "horizon_days", name=op.f("pk_labels_daily")
        ),
    )


def downgrade() -> None:
    op.drop_table("labels_daily")
    op.drop_table("features_daily")
