"""Intraday Signals workspace: versioned definitions on strategy_versions (additive), backtest_runs, intraday_signals."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0049_intraday_signals_lab"
down_revision: Union[str, None] = "0048_valuation_provider_inputs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    for name, col in (
        ("schema_version", sa.String(40)), ("source_type", sa.String(16)), ("definition", JSON_DOCUMENT), ("definition_hash", sa.String(64)),
        ("source_artifact", JSON_DOCUMENT), ("locked_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("strategy_versions", sa.Column(name, col, nullable=True))
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False), sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(12), nullable=False), sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config", JSON_DOCUMENT, nullable=False), sa.Column("definition_snapshot", JSON_DOCUMENT, nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False), sa.Column("provenance", JSON_DOCUMENT, nullable=True),
        sa.Column("result", JSON_DOCUMENT, nullable=True), sa.Column("error", sa.Text(), nullable=True),
        sa.Column("engine_version", sa.String(40), nullable=False), sa.Column("indicator_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_backtest_runs_status"),
    )
    op.create_index("ix_backtest_runs_user_created", "backtest_runs", ["user_id", "created_at"])
    op.create_index("ix_backtest_runs_strategy", "backtest_runs", ["strategy_id", "strategy_version"])
    op.create_table(
        "intraday_signals",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(12), nullable=False), sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(12), nullable=False), sa.Column("suppression_reason", sa.String(120), nullable=True),
        sa.Column("timeframe", sa.String(8), nullable=False), sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price", sa.Numeric(24, 8), nullable=False), sa.Column("indicators", JSON_DOCUMENT, nullable=False),
        sa.Column("conditions_met", JSON_DOCUMENT, nullable=False), sa.Column("conditions_unmet", JSON_DOCUMENT, nullable=False),
        sa.Column("rationale", sa.String(500), nullable=False), sa.Column("data_freshness", JSON_DOCUMENT, nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("direction IN ('long', 'short')", name="ck_intraday_signals_direction"),
        sa.CheckConstraint("status IN ('new', 'active', 'expired', 'invalidated', 'suppressed')", name="ck_intraday_signals_status"),
        sa.UniqueConstraint("strategy_id", "dedupe_key", name="uq_intraday_signals_strategy_dedupe"),
    )
    op.create_index("ix_intraday_signals_user_created", "intraday_signals", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_intraday_signals_user_created", table_name="intraday_signals")
    op.drop_table("intraday_signals")
    op.drop_index("ix_backtest_runs_strategy", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_user_created", table_name="backtest_runs")
    op.drop_table("backtest_runs")
    for name in ("locked_at", "source_artifact", "definition_hash", "definition", "source_type", "schema_version"):
        op.drop_column("strategy_versions", name)
