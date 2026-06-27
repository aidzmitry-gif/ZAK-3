"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class RfqAward(BaseModel):
    bid_id: int
