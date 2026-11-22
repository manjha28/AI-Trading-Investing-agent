"""Shadow record of the Nexus v2 Gate -> Quality -> Timing decision, stored BESIDE v1 (the brief and the daily report keep showing v1 while ``pipeline_v2_enabled`` is off). One row per research run per
stock; ``payload`` holds, per horizon, the v1 recommendation, the v2 decision, the stage that failed, the levels, grade, factor rank and the provisional blue-sky markers."""
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class PipelineV2Shadow(Base):
    __tablename__ = "pipeline_v2_shadow"
    __table_args__ = (Index("ix_pipeline_v2_shadow_instrument_time", "instrument_id", "computed_at"), Index("ix_pipeline_v2_shadow_user_time", "user_id", "computed_at"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    brief_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    as_of_candle: Mapped[date | None] = mapped_column(Date)
    v2_buy_horizons: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relaxation_dependent_ladders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # ladders that exist only because of the provisional near-52-week-high blue-sky rule
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
