"""ORM-модели модуля Procurement (схема ``procurement.*``)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false,
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
    supplier: Mapped[str] = mapped_column(String(255))  # легаси-строка (back-compat)
    supplier_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на procurement.supplier (приоритетный)
    flag: Mapped[str] = mapped_column(String(8), default="", server_default="")
    item: Mapped[str] = mapped_column(String(255))
    qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    priority: Mapped[str] = mapped_column(String(32), default="Средний", server_default="Средний")
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")
    stage: Mapped[str] = mapped_column(String(32), default="need", server_default="need")
    due_date: Mapped[str | None] = mapped_column(String(32))
    insight: Mapped[str] = mapped_column(String(400), default="", server_default="")
    # источник заявки: "" (ручная) / "deficit" (автозаявка по сигналу дефицита склада, wms.stock.low)
    origin: Mapped[str] = mapped_column(String(32), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SupplierClaim(Base):
    """Претензия поставщику. Создаётся автоматически при браке в производстве
    (событие ``production.scrap``) ИЛИ вручную закупщиком (``POST /procurement/claims``).

    Поставщик в авто-событии не приходит (дефект найден на сборке): претензия открывается
    без поставщика, ``status="open"``; закупщик проставит поставщика (``supplier_id``) и
    закроет её через ``PATCH /procurement/claims`` (resolved/rejected → событие).
    """

    __tablename__ = "supplier_claim"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(255), default="", server_default="")  # легаси-строка
    supplier_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на procurement.supplier
    item: Mapped[str] = mapped_column(String(255), default="", server_default="")
    reason: Mapped[str] = mapped_column(String(400), default="", server_default="")
    order_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # тип претензии: брак / недопоставка / пересорт / срок (свободный, не Enum БД)
    claim_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    qty_affected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    amount_byn: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # заявленная сумма претензии
    resolution: Mapped[str] = mapped_column(String(500), default="", server_default="")  # как урегулировано
    status: Mapped[str] = mapped_column(String(32), default="open", server_default="open")
    source: Mapped[str] = mapped_column(String(32), default="production", server_default="production")
    entity_ref: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# Жизненный цикл заказа: draft (черновик, не размещён) → ordered → shipped → customs → received;
# cancelled — отказ из любого открытого состояния. Открытый заказ (в пути, sales вычитает) —
# только ordered/shipped/customs (draft/received/cancelled НЕ открыты).
ORDER_STATUSES = ("draft", "ordered", "shipped", "customs", "received", "cancelled")
OPEN_ORDER_STATUSES = ("ordered", "shipped", "customs")
RECEIVED_ORDER_STATUS = "received"
# ранг для валидации переходов: вперёд (со скипами) можно, назад — нельзя (cancelled — отдельно)
ORDER_RANK = {"draft": 0, "ordered": 1, "shipped": 2, "customs": 3, "received": 4}


class PurchaseOrder(Base):
    """Размещённый заказ поставщику («машина»/контейнер): шапка + позиции по ``sku_code``.

    Отделён от воронки ``PurchaseRequest`` (пред-заказный sourcing): ``PurchaseOrder`` —
    уже размещённый заказ с ETA и сквозным статусом (``ORDER_STATUSES``). Открытый заказ
    (``OPEN_ORDER_STATUSES``) даёт продажам «в пути» по номенклатуре. На приёмке (``received``)
    общий движок ``allocate_landed_cost`` разносит фрахт на позиции → строки ``LandedCost`` per-SKU.
    """

    __tablename__ = "purchase_order"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    supplier: Mapped[str] = mapped_column(String(255), default="", server_default="")  # легаси-строка
    supplier_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на procurement.supplier
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    eta_date: Mapped[date | None] = mapped_column(Date)  # ожидаемое прибытие (ETA)
    # фактическая дата приёмки (статус → received); None пока заказ открыт — основа своевременности
    received_at: Mapped[datetime | None] = mapped_column(DateTime)
    # план сбора машины (Китай→Минск): способ перевозки (шаблон длительностей) + дедлайн «В Минске до»
    transport_method_code: Mapped[str | None] = mapped_column(String(32))  # soft-ref на transport_method.code
    target_arrival_date: Mapped[date | None] = mapped_column(Date)  # «В Минске до» — якорь обратного плана
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


class Supplier(Base):
    """Профиль поставщика закупок: рейтинг/условия/ЛПР. НЕ дубль карточки контрагента —
    эталон контрагента в MDM ядра; ``unp`` — soft-ref на MDM по УНП (без FK).
    Здесь живут именно закупочные атрибуты (оплата/срок/incoterms/статус)."""

    __tablename__ = "supplier"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    unp: Mapped[str] = mapped_column(String(32), default="", server_default="")  # soft-ref на MDM-контрагента
    country: Mapped[str] = mapped_column(String(64), default="", server_default="")
    flag: Mapped[str] = mapped_column(String(8), default="", server_default="")
    contact_person: Mapped[str] = mapped_column(String(128), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(64), default="", server_default="")
    email: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # условия оплаты, напр. «30% предоплата / 70% по факту»
    payment_terms: Mapped[str] = mapped_column(String(255), default="", server_default="")
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    incoterms: Mapped[str] = mapped_column(String(16), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")  # active/blocked
    notes: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Rfq(Base):
    """Запрос цен (тендер закупки): по позиции/номенклатуре собираем предложения поставщиков."""

    __tablename__ = "rfq"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    item: Mapped[str] = mapped_column(String(255), default="", server_default="")
    sku_code: Mapped[str] = mapped_column(String(64), default="", server_default="")  # soft-ref на 1С-код
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("1"), server_default="1")
    request_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на purchase_request
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")  # open/awarded/cancelled
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RfqBid(Base):
    """Предложение поставщика на запрос цен (RFQ): цена/срок/incoterms; победитель — ``is_winner``."""

    __tablename__ = "rfq_bid"
    __table_args__ = (
        Index("ix_rfq_bid_rfq", "rfq_id"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("procurement.rfq.id", ondelete="CASCADE"))
    supplier_id: Mapped[int | None] = mapped_column(Integer)  # soft-ref на procurement.supplier
    price_byn: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    incoterms: Mapped[str] = mapped_column(String(16), default="", server_default="")
    note: Mapped[str] = mapped_column(String(400), default="", server_default="")
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TransportMethod(Base):
    """Способ перевозки = шаблон длительностей этапов (дни) для плана машины.

    Редактируемый справочник: ``durations`` — {stage: дни} по этапам ``plan.SHIPMENT_STAGES``.
    Дефолты (Контейнер ≈112 дн / Машина ≈83 дн) сидятся из ``plan.DEFAULT_METHODS``; на
    конкретной машине длительность этапа можно переопределить (см. ``PurchaseOrderMilestone``).
    """

    __tablename__ = "transport_method"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # container / truck / …
    name: Mapped[str] = mapped_column(String(64))
    durations: Mapped[dict] = mapped_column(JSON, default=dict)  # {stage: дни}
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PurchaseOrderMilestone(Base):
    """Веха графика сбора машины: этап + план/факт даты (обратный waterfall от «В Минске до»).

    Одна строка на (заказ, этап). ``planned_date`` пересчитывается из шаблона способа перевозки
    при планировании; ``duration_days`` копируется из шаблона и правится на машине; ``actual_date`` —
    факт (приходит из событий: приёмка ⑦ из ``procurement.received``, отгрузка/таможня — из логистики).
    """

    __tablename__ = "purchase_order_milestone"
    __table_args__ = (
        UniqueConstraint("order_id", "stage"),  # одна веха каждого этапа на заказ
        Index("ix_purchase_order_milestone_order", "order_id"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("procurement.purchase_order.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(32))  # id этапа из plan.SHIPMENT_STAGES
    seq: Mapped[int] = mapped_column(Integer, default=0)  # порядок этапа
    duration_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    planned_date: Mapped[date | None] = mapped_column(Date)
    actual_date: Mapped[date | None] = mapped_column(Date)


class ShipRequirement(Base):
    """Требование клиента к сроку: крайняя дата отгрузки (срок поставки) по (сделка, sku).

    Локальный кэш закупок, наполняется из продаж по событию ``sales.deal.ship_deadline.set``
    (sales → procurement). Машина планируется так, чтобы прийти в Минск к самому раннему сроку
    своих позиций минус буфер «последней мили» — иначе риск срыва отгрузки и штрафа.
    ``ship_deadline`` — сырая строка из продаж; ``ship_deadline_date`` — разобранная дата (для
    расчёта; None, если формат не распознан). soft-ref на sales.deal по ``deal_id`` (без FK).
    """

    __tablename__ = "ship_requirement"
    __table_args__ = (
        UniqueConstraint("deal_id", "sku_code"),  # одно требование на (сделка, номенклатура)
        Index("ix_ship_requirement_sku", "sku_code"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(Integer)  # soft-ref на sales.deal (без cross-schema FK)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    counterparty: Mapped[str] = mapped_column(String(255), default="", server_default="")
    sku_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    ship_deadline: Mapped[str | None] = mapped_column(String(32))  # сырой срок клиента (как в продажах)
    ship_deadline_date: Mapped[date | None] = mapped_column(Date)  # разобранная дата (для расчёта)
    penalty_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # %/день просрочки
    penalty_cap_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # потолок % от суммы
    penalty_terms: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
