"""Pydantic-схемы модуля Procurement."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PurchaseRequestCreate(BaseModel):
    supplier: str
    item: str
    qty: int = 1
    status: str = "new"


class PurchaseRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier: str
    item: str
    qty: int
    status: str
