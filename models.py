"""ORM-модели модуля Procurement (схема ``procurement.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class PurchaseRequest(Base):
    """Закупка в воронке: товар, поставщик, сумма, стадия sourcing-цикла.

    Стадия (`stage`) ведёт закупку от потребности до завершения (см. ``stages.py``).
    Переход в «Приёмку / QC» (``qc``) публикует ``procurement.received`` → приход на склад.
    """

    __tablename__ = "purchase_request"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    supplier: Mapped[str] = mapped_column(String(255))
    flag: Mapped[str] = mapped_column(String(8), default="", server_default="")
    item: Mapped[str] = mapped_column(String(255))
    qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    priority: Mapped[str] = mapped_column(String(32), default="Средний", server_default="Средний")
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")
    stage: Mapped[str] = mapped_column(String(32), default="need", server_default="need")
    due_date: Mapped[str | None] = mapped_column(String(32))
    insight: Mapped[str] = mapped_column(String(400), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SupplierClaim(Base):
    """Претензия поставщику. Создаётся автоматически при браке в производстве
    (событие ``production.scrap``) — замыкает цикл ОТК → закупки.

    Поставщик в событии не приходит (дефект найден на сборке, привязку к PO делает
    закупщик): претензия открывается без поставщика, ``status="open"``; закупщик
    позже проставляет поставщика и закрывает её через ``PATCH /procurement/claims``.
    """

    __tablename__ = "supplier_claim"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(255), default="", server_default="")
    item: Mapped[str] = mapped_column(String(255), default="", server_default="")
    reason: Mapped[str] = mapped_column(String(400), default="", server_default="")
    order_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), default="open", server_default="open")
    source: Mapped[str] = mapped_column(String(32), default="production", server_default="production")
    entity_ref: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
