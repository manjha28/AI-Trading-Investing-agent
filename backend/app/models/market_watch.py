from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MarketWatchExtra(Base):
    """An instrument the user added to their Market watch master list although it is outside the Dhan-mapped
    NSE universe. Per user; separate from watchlists (a starred symbol is not necessarily in the master list)."""

    __tablename__ = "market_watch_extras"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", name="uq_market_watch_extra"),
        Index("ix_market_watch_extras_user", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
