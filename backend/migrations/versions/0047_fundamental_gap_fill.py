"""Add per-field provenance, statement basis and extra metrics to instrument_fundamentals (additive)."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0047_fundamental_gap_fill"
down_revision: Union[str, None] = "0046_ai_stock_analyses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instrument_fundamentals", sa.Column("statement_basis", sa.String(16), nullable=True))
    op.add_column("instrument_fundamentals", sa.Column("field_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("instrument_fundamentals", sa.Column("extra_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("instrument_fundamentals", "extra_metrics")
    op.drop_column("instrument_fundamentals", "field_sources")
    op.drop_column("instrument_fundamentals", "statement_basis")
