"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Статус заказа — закрытый набор (опечатка тихо пропустила бы фиксацию landed cost на приёмке).
# Должен совпадать с ORDER_STATUSES в models.py.
OrderStatus = Literal["draft", "ordered", "shipped", "customs", "received", "cancelled"]
# Статус претензии — закрытый набор (свободная строка пропустила бы опечатку мимо событий).
ClaimStatus = Literal["open", "resolved", "rejected"]


class PurchaseRequestCreate(BaseModel):
    supplier: str
    supplier_id: int | None = None  # soft-ref на procurement.supplier (приоритетный над строкой)
    flag: str = ""
    item: str
    qty: int = 1
    amount: float = 0
    priority: str = "Средний"
    owner: str = ""
    stage: str = "need"
    number: str = ""
    due_date: str | None = None
    insight: str = ""
    origin: str = ""  # "" (ручная) / "deficit" (автозаявка по сигналу дефицита склада)


class PurchaseRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    supplier: str
    supplier_id: int | None = None
    flag: str = ""
    item: str
    qty: int
    amount: float
    priority: str
    owner: str
    stage: str
    due_date: str | None = None
    insight: str = ""
    origin: str = ""  # источник заявки (для бейджа «Авто: дефицит склада» на доске)


class DeficitRequestIn(BaseModel):
    """Запрос на создание автозаявки из сигнала дефицита склада (тонкий debug/manual вход;
    штатный путь — подписка на ``wms.stock.low``). Идемпотентно по (origin='deficit', позиция)."""

    sku_code: str
    sku_title: str = ""
    warehouse: str = "Главный"
    deficit: float = Field(0, ge=0)
    reorder_qty: float = Field(0, ge=0)
    supplier_id: int | None = None


class StageUpdate(BaseModel):
    stage: str


# ───────────────────────── Открытый заказ (PurchaseOrder) ─────────────────────────


class PurchaseOrderLineIn(BaseModel):
    sku_code: str
    qty: float = Field(1, ge=0)
    goods_value_byn: float = Field(0, ge=0)
    weight: float = Field(0, ge=0)
    volume: float = Field(0, ge=0)


class PurchaseOrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_code: str
    qty: float
    goods_value_byn: float
    weight: float
    volume: float


class PurchaseOrderCreate(BaseModel):
    supplier: str = ""
    supplier_id: int | None = None  # soft-ref на procurement.supplier
    number: str = ""
    status: OrderStatus = "draft"  # новый заказ — черновик до размещения (не «в пути»)
    eta_date: date | None = None
    freight_byn: float = Field(0, ge=0)
    lines: list[PurchaseOrderLineIn] = []


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    supplier: str
    supplier_id: int | None = None
    status: str
    eta_date: date | None = None
    received_at: datetime | None = None  # факт приёмки (статус → received); None пока открыт
    freight_byn: float
    lines: list[PurchaseOrderLineOut] = []


class PurchaseOrderStatusUpdate(BaseModel):
    status: OrderStatus


class PurchaseOrderHeaderUpdate(BaseModel):
    """Правка шапки заказа в редакторе машины (фрахт/ETA/поставщик). Статус — отдельным
    эндпоинтом (машина состояний). Все поля опциональны (PATCH)."""

    supplier: str | None = None
    supplier_id: int | None = None
    eta_date: date | None = None
    freight_byn: float | None = Field(None, ge=0)


# ───────────────────── Предв. себестоимость (Расчёт Китай) ─────────────────────


class CostEstimateLineIn(BaseModel):
    sku_code: str
    path: Literal["cny", "usd"] = "cny"  # валюта поставщика (CNY-путь / USD-путь)
    price: float = Field(ge=0)  # цена единицы у поставщика в валюте path
    qty: float = Field(1, ge=0)
    weight: float = Field(0, ge=0)  # кг брутто на единицу
    duty_pct: float | None = Field(None, ge=0)  # ставка пошлины по ТН ВЭД; None → default_duty_pct
    util: float = Field(0, ge=0)  # утильсбор BYN на единицу (техника)


class CostRatesIn(BaseModel):
    usd_byn: float = 0
    # CNY-путь: курсы для пересчёта юаней; для USD-only запроса не нужны (дефолт 0)
    cny_rub: float = 0
    rub_byn: float = 0
    usd_rub: float = 0
    commission_pct: float = 0
    insurance_pct: float = 0
    freight_usd_per_kg: float = 0
    default_duty_pct: float = 0
    fx_buffer_pct: float = 10  # буфер курса, мин. 10 (защита прибыли от колебаний)


class CostEstimateRequest(BaseModel):
    lines: list[CostEstimateLineIn]
    rates: CostRatesIn
    operation_date: date | None = None


class CostEstimateLineOut(BaseModel):
    sku_code: str
    goods_byn: float
    commission_byn: float
    insurance_byn: float
    freight_byn: float
    duty_byn: float
    util_byn: float
    unit_landed_cost_byn: float


class CostEstimateOut(BaseModel):
    fx_quotes: dict = Field(default_factory=dict)
    lines: list[CostEstimateLineOut]
    total_landed_byn: float


# ───────────────────────── Претензии поставщикам ─────────────────────────


class SupplierClaimCreate(BaseModel):
    """Ручное заведение претензии закупщиком (не из брака производства)."""

    supplier: str = ""
    supplier_id: int | None = None
    item: str = ""
    reason: str = ""
    order_code: str = ""
    claim_type: str = ""  # брак / недопоставка / пересорт / срок
    qty_affected: int = Field(0, ge=0)
    amount_byn: float | None = Field(None, ge=0)


class SupplierClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier: str = ""
    supplier_id: int | None = None
    item: str = ""
    reason: str = ""
    order_code: str = ""
    claim_type: str = ""
    qty_affected: int = 0
    amount_byn: float | None = None
    resolution: str = ""
    status: str
    source: str
    entity_ref: str = ""


class SupplierClaimUpdate(BaseModel):
    supplier: str | None = None
    supplier_id: int | None = None
    status: ClaimStatus | None = None
    resolution: str | None = None


# ───────────────────────── Справочник поставщиков (Supplier) ─────────────────────────


class SupplierBase(BaseModel):
    name: str
    unp: str = ""  # soft-ref на MDM-контрагента (провенанс)
    country: str = ""
    flag: str = ""
    contact_person: str = ""
    phone: str = ""
    email: str = ""
    payment_terms: str = ""
    lead_time_days: int | None = None
    incoterms: str = ""
    status: str = "active"  # active / blocked
    notes: str = ""


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: str | None = None
    unp: str | None = None
    country: str | None = None
    flag: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    payment_terms: str | None = None
    lead_time_days: int | None = None
    incoterms: str | None = None
    status: str | None = None
    notes: str | None = None


class SupplierOut(SupplierBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


# ───────────────────────── RFQ / тендер закупки ─────────────────────────


class RfqCreate(BaseModel):
    item: str = ""
    sku_code: str = ""
    qty: float = Field(1, ge=0)
    request_id: int | None = None
    due_date: date | None = None


class RfqBidIn(BaseModel):
    supplier_id: int | None = None
    price_byn: float = Field(ge=0)
    lead_time_days: int | None = None
    incoterms: str = ""
    note: str = ""


class RfqBidOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rfq_id: int
    supplier_id: int | None = None
    price_byn: float
    lead_time_days: int | None = None
    incoterms: str = ""
    note: str = ""
    is_winner: bool = False


class RfqOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item: str = ""
    sku_code: str = ""
    qty: float
    request_id: int | None = None
    status: str
    due_date: date | None = None
    bids: list[RfqBidOut] = []
    best_bid_id: int | None = None  # bid с минимальной ценой (для подсветки лучшей)
    created_order_id: int | None = None  # черновик PO, созданный при award (P7); None — не создан


class RfqAward(BaseModel):
    bid_id: int


# ───────────────────── План сбора машины (этапы Китай → Минск) ─────────────────────


class TransportMethodOut(BaseModel):
    code: str
    name: str
    durations: dict[str, int]  # {stage: дни}
    total_days: int
    active: bool = True


class TransportMethodUpdate(BaseModel):
    """Правка справочника способов перевозки (название / длительности этапов / активность)."""

    name: str | None = None
    durations: dict[str, int] | None = None
    active: bool | None = None


class MilestoneOut(BaseModel):
    stage: str
    title: str
    seq: int
    duration_days: int
    planned_date: date | None = None
    actual_date: date | None = None


class OrderPlanIn(BaseModel):
    """Запланировать машину: способ перевозки (шаблон длительностей) + дедлайн «В Минске до».
    План этапов — обратный waterfall от ``target_arrival_date``. Дата обязательна: клиентские требования без scoped facade не используются."""

    transport_method_code: str
    target_arrival_date: date  # explicit date; customer requirements are not authorized


class AtRiskDeal(BaseModel):
    """Сделка под риском срыва срока: план машины приходит позже крайней даты В Минске."""

    deal_id: int
    number: str = ""
    counterparty: str = ""
    sku_code: str = ""
    ship_deadline: str | None = None  # срок клиента (сырой)
    required_arrival: date | None = None  # срок − буфер (когда машина обязана быть в Минске)
    slack_days: int | None = None  # required_arrival − target (≥0 запас, <0 опоздание)
    penalty_rate_pct: float | None = None
    penalty_cap_pct: float | None = None
    penalty_terms: str | None = None


class OrderPlanOut(BaseModel):
    order_id: int
    transport_method_code: str | None = None
    target_arrival_date: date | None = None
    start_date: date | None = None  # «Спланирован заказ» (начало сбора)
    total_days: int = 0
    milestones: list[MilestoneOut] = []
    # ограничение и риск от срока клиента (sales → procurement)
    required_by: date | None = None  # самый ранний срок клиента среди позиций машины
    required_arrival: date | None = None  # required_by − буфер: крайняя дата «В Минске»
    slack_days: int | None = None  # required_arrival − target_arrival (<0 = опоздание)
    at_risk: bool = False  # план приходит позже крайней даты (или старт уже в прошлом)
    at_risk_deals: list[AtRiskDeal] = []


class EditorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def exact_decimal(cls, value, info):
        scales = {"qty": 2, "goods_value_byn": 2, "freight_byn": 2, "weight": 3, "volume": 4}
        if info.field_name in scales:
            scale = scales[info.field_name]
            if not isinstance(value, str) or not re.fullmatch(
                rf"(?:0|[1-9][0-9]{{0,{13-scale}}})(?:\.[0-9]{{1,{scale}}})?", value
            ):
                raise ValueError("Use a nonnegative exact decimal string within the stored precision")
            return Decimal(value)
        if isinstance(value, str):
            value = value.strip()
            if "\x00" in value:
                raise ValueError("NUL is forbidden")
        return value


class EditorLineInput(EditorInput):
    sku_code: str = Field(min_length=1, max_length=64)
    sku_id: int | None = Field(default=None, gt=0, le=2147483647)
    sku_title: str | None = Field(default=None, min_length=1, max_length=255)
    sku_unit: str | None = Field(default=None, min_length=1, max_length=16)
    qty: Decimal = Field(default=Decimal("1.00"), gt=0)
    goods_value_byn: Decimal = Decimal("0.00")
    weight: Decimal = Decimal("0.000")
    volume: Decimal = Decimal("0.0000")

    @model_validator(mode="after")
    def paired_sku_snapshot(self):
        fields = (self.sku_id, self.sku_title, self.sku_unit)
        if any(value is not None for value in fields) and not all(value is not None for value in fields):
            raise ValueError("SKU catalog ID, title and unit must be supplied together")
        return self


class EditorHeaderInput(EditorInput):
    supplier: str = Field(default="", max_length=255)
    supplier_id: int | None = Field(default=None, gt=0)
    eta_date: date | None = None
    freight_byn: Decimal = Decimal("0.00")


class OrderMutationOut(BaseModel):
    organization_id: int
    principal: str
    order_id: int
    action: Literal["status", "add_line", "delete_line", "header"]
    affected_line_id: int | None = None
    status: str
    received_at: datetime | None = None


class ScopedOrderPlanOut(BaseModel):
    organization_id: int
    order_id: int
    principal: str | None = None
    transport_method_code: str | None = None
    target_arrival_date: date | None = None
    start_date: date | None = None
    total_days: int
    milestones: list[MilestoneOut]
    customer_requirements_status: Literal["unverified"] = "unverified"
    required_by: None = None
    required_arrival: None = None
    slack_days: None = None
    at_risk: None = None
    at_risk_deals: list[AtRiskDeal] = Field(default_factory=list, max_length=0)
    schedule_start_in_past: bool | None = None


class EditorDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: int = Field(strict=True, gt=0, le=2147483647)


class EditorStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: OrderStatus


class EditorPlanInput(OrderPlanIn):
    model_config = ConfigDict(extra="forbid")

    @field_validator("target_arrival_date", mode="before")
    @classmethod
    def explicit_date(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Use explicit YYYY-MM-DD")
        return value
