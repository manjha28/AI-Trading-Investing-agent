"""Add separately stored, snapshot-referenced AI stock analyses."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0046_ai_stock_analyses"
down_revision: Union[str, None] = "0045_research_orchestration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_stock_analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "brief_id", sa.Uuid(),
            sa.ForeignKey("instrument_research_briefs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("instrument_id", sa.Uuid(), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "brief_id", "horizon", "model", "prompt_version",
            name="uq_ai_stock_analyses_cache_key",
        ),
    )
    op.create_index(
        "ix_ai_stock_analyses_instrument_created", "ai_stock_analyses", ["instrument_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_stock_analyses_instrument_created", table_name="ai_stock_analyses")
    op.drop_table("ai_stock_analyses")
