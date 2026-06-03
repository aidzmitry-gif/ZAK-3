"""ORM-модели модуля Procurement (схема ``procurement.*``)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class PurchaseRequest(Base):
    """Заявка на закупку: поставщик, позиция, количество, статус."""

    __tablename__ = "purchase_request"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(255))
    item: Mapped[str] = mapped_column(String(255))
    qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(32), default="new", server_default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
