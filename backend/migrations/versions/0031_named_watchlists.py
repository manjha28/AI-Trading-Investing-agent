"""Add named watchlists and preserve existing instruments.

Revision ID: 0031_named_watchlists
Revises: 0030_dhan_trade_identity
"""

from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision: str = "0031_named_watchlists"
down_revision: Union[str, None] = "0030_dhan_trade_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_watchlists_user_name"),
    )
    op.create_index(
        "ix_watchlists_user_created", "watchlists", ["user_id", "created_at"]
    )
    op.add_column(
        "watchlist_instruments",
        sa.Column("watchlist_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_watchlist_instruments_watchlist_id_watchlists",
        "watchlist_instruments",
        "watchlists",
        ["watchlist_id"],
        ["id"],
        ondelete="CASCADE",
    )

    bind = op.get_bind()
    existing_users = bind.execute(
        sa.text(
            "SELECT user_id, MIN(created_at) AS created_at "
            "FROM watchlist_instruments GROUP BY user_id"
        )
    ).all()
    for user_id, created_at in existing_users:
        watchlist_id = uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO watchlists (id, user_id, name, created_at) "
                "VALUES (:id, :user_id, :name, :created_at)"
            ),
            {
                "id": watchlist_id,
                "user_id": user_id,
                "name": "Default",
                "created_at": created_at or datetime.now(timezone.utc),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE watchlist_instruments SET watchlist_id = :watchlist_id "
                "WHERE user_id = :user_id"
            ),
            {"watchlist_id": watchlist_id, "user_id": user_id},
        )

    op.alter_column("watchlist_instruments", "watchlist_id", nullable=False)
    op.drop_constraint(
        "uq_watchlist_user_instrument",
        "watchlist_instruments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_watchlist_instrument",
        "watchlist_instruments",
        ["watchlist_id", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_watchlist_instrument", "watchlist_instruments", type_="unique"
    )
    op.create_unique_constraint(
        "uq_watchlist_user_instrument",
        "watchlist_instruments",
        ["user_id", "instrument_id"],
    )
    op.drop_constraint(
        "fk_watchlist_instruments_watchlist_id_watchlists",
        "watchlist_instruments",
        type_="foreignkey",
    )
    op.drop_column("watchlist_instruments", "watchlist_id")
    op.drop_index("ix_watchlists_user_created", table_name="watchlists")
    op.drop_table("watchlists")
