"""Delivery percentage per symbol and session from the NSE full bhavcopy (sec_bhavdata_full, DELIV_PER), kept for the symbols the app tracks. Additive (migration 0072). Missing is absent, never 0."""
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DeliveryPercentage(Base):
    __tablename__ = "delivery_percentages"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    series: Mapped[str] = mapped_column(String(4), primary_key=True)
    deliv_per: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="nse_sec_bhavdata_full")
