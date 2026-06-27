"""ORM-модели модуля Procurement (схема ``procurement.*``)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
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


# Статусы открытого заказа: размещён → отгружен → таможня → принят. Пока товар не принят
# (``received``), заказ «открыт» = в пути: sales вычитает его qty в нетто-доступности.
OPEN_ORDER_STATUSES = ("ordered", "shipped", "customs")
RECEIVED_ORDER_STATUS = "received"


class PurchaseOrder(Base):
    """Размещённый заказ поставщику («машина»/контейнер): шапка + позиции по ``sku_code``.

    Отделён от воронки ``PurchaseRequest`` (пред-заказный sourcing): ``PurchaseOrder`` —
    уже размещённый заказ с ETA и сквозным статусом. Открытый заказ (``OPEN_ORDER_STATUSES``)
    даёт продажам «в пути» по номенклатуре. На приёмке (``received``) общий движок
    ``allocate_landed_cost`` разносит фрахт/издержки на позиции → строки ``LandedCost`` per-SKU.
    """

    __tablename__ = "purchase_order"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    supplier: Mapped[str] = mapped_column(String(255), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="ordered", server_default="ordered")
    eta_date: Mapped[date | None] = mapped_column(Date)  # ожидаемое прибытие (ETA)
    # общий фрахт партии (BYN), разносится на позиции при приёмке (база — вес, иначе стоимость)
    freight_byn: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PurchaseOrderLine(Base):
    """Позиция заказа: номенклатура, кол-во, стоимость товара и базы распределения (вес/объём)."""

    __tablename__ = "purchase_order_line"
    __table_args__ = (
        Index("ix_purchase_order_line_order", "order_id"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("procurement.purchase_order.id", ondelete="CASCADE")
    )
    sku_code: Mapped[str] = mapped_column(String(64))  # soft-ref на 1С-код
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("1"), server_default="1")
    # стоимость товара позиции (без издержек), BYN — база landed cost
    goods_value_byn: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    weight: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"), server_default="0")  # кг брутто
    volume: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"), server_default="0")  # м³


class LandedCost(Base):
    """Себестоимость единицы номенклатуры, доведённой до склада (BYN) — результат
    распределения издержек заказа на позицию (см. ``core.services.landed_cost``).

    Источник истины себестоимости для продаж: ``quote_line`` читает её через read-only
    фасад ядра ``core.services.landed_cost.last_landed_cost(session, sku_code)`` и хранит
    снапшот у себя (без cross-schema FK, soft-ref по ``sku_code`` — как ``stock``).

    Минимальный срез: фрахт заказа разносится на позиции; пошлина по ТН ВЭД, два FX-курса и
    пересчёт ``estimated→actual`` — Горизонт 2 (docs/landed-cost.md).
    """

    __tablename__ = "landed_cost"
    __table_args__ = (
        # повторная приёмка не плодит дубль — upsert по (номенклатура, заказ)
        UniqueConstraint("sku_code", "purchase_order_id"),
        # выборка «последняя себестоимость по номенклатуре» (order by fixed_at desc limit 1)
        Index("ix_landed_cost_sku_fixed", "sku_code", "fixed_at"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_code: Mapped[str] = mapped_column(String(64))  # soft-ref на 1С-код, без FK
    purchase_order_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на заказ (провенанс)
    shipment_id: Mapped[str] = mapped_column(String(64), default="", server_default="")  # PO/рейс — провенанс
    # себестоимость единицы в BYN (конверсию делает закупка, не sales); храним 4 знака
    unit_landed_cost_byn: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stage: Mapped[str] = mapped_column(String(16), default="estimated", server_default="estimated")
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))  # курс НБ РБ, применённый к cost
    fx_date: Mapped[date | None] = mapped_column(Date)  # дата курса — по ней sales судит «устарела ли»
    fx_rate_basis: Mapped[str | None] = mapped_column(String(8))  # смысл fx_date: po/gtd/payment
    # дата фиксации cost (обновляется при пересчёте) — по ней выбираем «последнюю»
    fixed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
