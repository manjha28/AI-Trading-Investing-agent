"""Widen instrument uniqueness to include contract identity.

Replaces the (venue, symbol) key, which would forbid a future-contract row
sharing a symbol with its equity row. Uses NULLS NOT DISTINCT so equity rows
(NULL expiry, strike and option_type) still collide with each other.

Revision ID: 0035_instrument_identity_uniq
Revises: 0034_instrument_venue_uniq
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0035_instrument_identity_uniq"
down_revision: Union[str, None] = "0034_instrument_venue_uniq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _duplicate_group_count(columns: str) -> int:
    """Count key groups with more than one row. GROUP BY treats NULLs as equal, matching NULLS NOT DISTINCT."""
    return int(
        op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM ("
                f"SELECT {columns} FROM instruments GROUP BY {columns} HAVING COUNT(*) > 1"
                ") AS duplicate_groups"
            )
        ).scalar_one()
    )


def upgrade() -> None:
    duplicates = _duplicate_group_count(
        "venue, symbol, asset_class, expiry, option_type, strike"
    )
    if duplicates:
        raise RuntimeError(
            f"0035 aborted: {duplicates} instrument key group(s) violate uq_instruments_identity. "
            "Resolve them by re-parenting, not deleting, then re-run."
        )
    op.create_index(
        "uq_instruments_identity",
        "instruments",
        ["venue", "symbol", "asset_class", "expiry", "option_type", "strike"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.drop_index("uq_instruments_venue_symbol", table_name="instruments")


def downgrade() -> None:
    duplicates = _duplicate_group_count("venue, symbol")
    if duplicates:
        raise RuntimeError(
            f"0035 downgrade aborted: {duplicates} (venue, symbol) group(s) would violate "
            "uq_instruments_venue_symbol. Re-parent contract rows before downgrading."
        )
    op.create_index(
        "uq_instruments_venue_symbol",
        "instruments",
        ["venue", "symbol"],
        unique=True,
    )
    op.drop_index("uq_instruments_identity", table_name="instruments")
