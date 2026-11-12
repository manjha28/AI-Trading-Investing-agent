"""Add valuation_provider_inputs: per-provider valuation inputs for the Dhan -> Yahoo -> Screener chain (additive)."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0048_valuation_provider_inputs"
down_revision: Union[str, None] = "0047_fundamental_gap_fill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "valuation_provider_inputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("metric", sa.String(48), nullable=False),
        sa.Column("value", sa.Numeric(30, 10), nullable=True),
        sa.Column("text_value", sa.String(200), nullable=True),
        sa.Column("unit", sa.String(40), nullable=True),
        sa.Column("unit_assumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reporting_period", sa.String(40), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "provider", "metric", name="uq_valuation_inputs_instrument_provider_metric"),
    )
    op.create_index("ix_valuation_inputs_group", "valuation_provider_inputs", ["provider", "metric", "text_value"])


def downgrade() -> None:
    op.drop_index("ix_valuation_inputs_group", table_name="valuation_provider_inputs")
    op.drop_table("valuation_provider_inputs")
