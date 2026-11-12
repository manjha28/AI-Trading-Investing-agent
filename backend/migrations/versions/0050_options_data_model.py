"""Options research data model (additive): option_contracts, option_quotes, intraday_signals.option."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0050_options_data_model"
down_revision: Union[str, None] = "0049_intraday_signals_lab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "option_contracts",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("underlying", sa.String(32), nullable=False), sa.Column("trading_symbol", sa.String(80), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=True), sa.Column("expiry", sa.DateTime(timezone=True), nullable=False), sa.Column("strike", sa.Numeric(18, 4), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False), sa.Column("lot_size", sa.Integer(), nullable=True), sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True), sa.Column("instrument_id", sa.Uuid(), nullable=True), sa.Column("source", sa.String(32), nullable=False),
        sa.Column("master_version", sa.String(64), nullable=False), sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("option_type IN ('CE', 'PE')", name="ck_option_contracts_type"),
        sa.UniqueConstraint("underlying", "expiry", "strike", "option_type", "valid_from", name="uq_option_contracts_identity"),
    )
    op.create_index("ix_option_contracts_lookup", "option_contracts", ["underlying", "expiry", "valid_from"])
    op.create_table(
        "option_quotes",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("contract_id", sa.Uuid(), nullable=False), sa.Column("quote_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False), sa.Column("bid", sa.Numeric(18, 4), nullable=True), sa.Column("ask", sa.Numeric(18, 4), nullable=True),
        sa.Column("bid_qty", sa.Integer(), nullable=True), sa.Column("ask_qty", sa.Integer(), nullable=True), sa.Column("ltp", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True), sa.Column("open_interest", sa.Integer(), nullable=True), sa.Column("implied_volatility", sa.Numeric(12, 6), nullable=True),
        sa.Column("delta", sa.Numeric(12, 6), nullable=True), sa.Column("greeks_source", sa.String(12), nullable=True), sa.Column("underlying_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["option_contracts.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("greeks_source IS NULL OR greeks_source IN ('provider', 'calculated')", name="ck_option_quotes_greeks_source"),
        sa.UniqueConstraint("contract_id", "quote_time", "source", name="uq_option_quotes_contract_time_source"),
    )
    op.create_index("ix_option_quotes_time", "option_quotes", ["contract_id", "quote_time"])
    op.add_column("intraday_signals", sa.Column("option", JSON_DOCUMENT, nullable=True))


def downgrade() -> None:
    op.drop_column("intraday_signals", "option")
    op.drop_index("ix_option_quotes_time", table_name="option_quotes")
    op.drop_table("option_quotes")
    op.drop_index("ix_option_contracts_lookup", table_name="option_contracts")
    op.drop_table("option_contracts")
