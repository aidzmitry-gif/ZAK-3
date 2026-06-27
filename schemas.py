"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Статус заказа — закрытый набор (опечатка тихо пропустила бы фиксацию landed cost на приёмке).
# Должен совпадать с OPEN_ORDER_STATUSES + RECEIVED_ORDER_STATUS в models.py.
OrderStatus = Literal["ordered", "shipped", "customs", "received"]


class PurchaseRequestCreate(BaseModel):
    supplier: str
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
    qty: float = 1
    goods_value_byn: float = 0
    weight: float = 0
    volume: float = 0


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
    number: str = ""
    status: OrderStatus = "ordered"
    eta_date: date | None = None
    freight_byn: float = 0
    lines: list[PurchaseOrderLineIn] = []


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    supplier: str
    status: str
    eta_date: date | None = None
    freight_byn: float
    lines: list[PurchaseOrderLineOut] = []


class PurchaseOrderStatusUpdate(BaseModel):
    status: OrderStatus


# ───────────────────── Предв. себестоимость (Расчёт Китай) ─────────────────────


class CostEstimateLineIn(BaseModel):
    sku_code: str
    path: Literal["cny", "usd"] = "cny"  # валюта поставщика (CNY-путь / USD-путь)
    price: float  # цена единицы у поставщика в валюте path
    qty: float = 1
    weight: float = 0  # кг брутто на единицу
    duty_pct: float | None = None  # ставка пошлины по ТН ВЭД; None → default_duty_pct
    util: float = 0  # утильсбор BYN на единицу (техника)


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


class SupplierClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier: str = ""
    item: str = ""
    reason: str = ""
    order_code: str = ""
    status: str
    source: str
    entity_ref: str = ""


class SupplierClaimUpdate(BaseModel):
    supplier: str | None = None
    status: str | None = None
