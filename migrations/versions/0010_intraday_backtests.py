"""Intraday backtests (sprint S5i).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-25

- backtest_runs: one row per backtest of a rule strategy, a baseline or a model, with the
  capital, the slippage assumed, and the result after every charge.
- backtest_trades: every simulated trade of a run, so a result can be checked trade by trade.
- model_versions.kind gains 'intraday' (one model per side over intraday features; its
  horizon column counts one-minute bars rather than trading days).

Nothing in these tables is a real order: the project never places any.

mi_app gets its rights from the default privileges set in 0002 (tables and identity
sequences). Never run downgrade on real data (only in CI): it drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("label_version", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=True),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("capital", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("slippage_bps", sa.Float(), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False),
        sa.Column("hit_rate", sa.Float(), nullable=True),
        sa.Column("net_profit", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("costs_paid", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=False),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("days_without_trade", sa.Integer(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('strategy', 'model', 'baseline')", name=op.f("ck_backtest_runs_kind")
        ),
        sa.CheckConstraint("capital > 0", name=op.f("ck_backtest_runs_capital_positive")),
        sa.CheckConstraint("to_date >= from_date", name=op.f("ck_backtest_runs_date_order")),
        sa.CheckConstraint(
            "trades >= 0 AND days >= 0 AND days_without_trade >= 0",
            name=op.f("ck_backtest_runs_counts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_runs")),
    )
    op.create_index(
        op.f("ix_backtest_runs_kind"), "backtest_runs", ["kind", "name", "period"], unique=False
    )
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("entry", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("target", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("stop", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("bars_held", sa.Integer(), nullable=False),
        sa.Column("exit_reason", sa.Text(), nullable=False),
        sa.Column("gross_profit", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("costs", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("net_profit", sa.Numeric(precision=16, scale=2), nullable=False),
        sa.Column("probability", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "exit_reason IN ('target', 'stop', 'time_limit', 'session_end')",
            name=op.f("ck_backtest_trades_exit_reason"),
        ),
        sa.CheckConstraint("side IN ('long', 'short')", name=op.f("ck_backtest_trades_side")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_backtest_trades_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_backtest_trades_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_backtest_trades_run_id_backtest_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trades")),
    )
    op.create_index(
        op.f("ix_backtest_trades_run_id"), "backtest_trades", ["run_id", "trade_date"], unique=False
    )

    op.drop_constraint(op.f("ck_model_versions_kind"), "model_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_model_versions_kind"),
        "model_versions",
        "kind IN ('sector', 'pooled', 'intraday')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_model_versions_kind"), "model_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_model_versions_kind"), "model_versions", "kind IN ('sector', 'pooled')"
    )
    op.drop_index(op.f("ix_backtest_trades_run_id"), table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_index(op.f("ix_backtest_runs_kind"), table_name="backtest_runs")
    op.drop_table("backtest_runs")
