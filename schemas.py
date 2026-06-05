"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

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
