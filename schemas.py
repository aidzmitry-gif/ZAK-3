"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


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
    status: str = "ordered"
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
    status: str


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
