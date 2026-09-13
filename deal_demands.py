"""Demand register connecting CRM deal items to supplier-order quantities.

The register is deliberately separate from warehouse reservations.  A demand may be
created before a supplier order exists, while an allocation records the exact part of
an open supplier-order line assigned to that customer.  No stock movement is created
by this module.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import Sku
from modules.procurement.expected_reservations import ExpectedReservation, ExpectedReservationEvent
from modules.procurement.expected_reservations import _projection as expected_projection
from modules.procurement.models import OPEN_ORDER_STATUSES, PurchaseOrder, PurchaseOrderLine
from modules.procurement.ownership import PurchaseOwnership
from modules.procurement.receipt_documents import scoped
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.models import Deal, DealDocument, DealItem


class DealProcurementDemand(Base):
    """Immutable customer demand for a SKU from a CRM deal."""

    __tablename__ = "deal_procurement_demand"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key", name="deal_demand_request"),
        CheckConstraint("organization_id > 0 AND deal_id > 0 AND deal_item_id > 0", name="deal_demand_positive_refs"),
        CheckConstraint("qty > 0 AND qty < 1000000000000", name="deal_demand_qty"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    deal_item_id: Mapped[int] = mapped_column(Integer, index=True)
    sku_id: Mapped[int] = mapped_column(Integer)
    sku_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    document_id: Mapped[int | None] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealProcurementAllocation(Base):
    """Append-only allocation of a demand to an exact supplier-order line."""

    __tablename__ = "deal_procurement_allocation"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key", name="deal_demand_allocation_request"),
        CheckConstraint("organization_id > 0 AND demand_id > 0 AND order_id > 0 AND order_line_id > 0", name="deal_allocation_positive_refs"),
        CheckConstraint("qty > 0 AND qty < 1000000000000", name="deal_allocation_qty"),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    demand_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_line_id: Mapped[int] = mapped_column(Integer, index=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    request_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(*_args):
    raise ValueError("Deal procurement history is immutable; append a new command")


for model in (DealProcurementDemand, DealProcurementAllocation):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)


class DemandCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    deal_item_id: int = Field(gt=0, strict=True)
    qty: str = Field(strict=True)
    document_id: int | None = Field(default=None, gt=0, strict=True)
    request_key: str = Field(strict=True, min_length=36, max_length=36)
    evidence: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("qty")
    @classmethod
    def exact_qty(cls, value: str) -> str:
        if not re.fullmatch(r"(?:0|[1-9]\d{0,11})\.\d{2}", value) or Decimal(value) <= 0:
            raise ValueError("Use a positive quantity with two decimal places")
        return value

    @field_validator("request_key")
    @classmethod
    def canonical_key(cls, value: str) -> str:
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Use a canonical request UUID") from exc
        return value


class AllocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order_id: int = Field(gt=0, strict=True)
    order_line_id: int = Field(gt=0, strict=True)
    qty: str = Field(strict=True)
    request_key: str = Field(strict=True, min_length=36, max_length=36)
    evidence: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("qty")
    @classmethod
    def exact_qty(cls, value: str) -> str:
        if not re.fullmatch(r"(?:0|[1-9]\d{0,11})\.\d{2}", value) or Decimal(value) <= 0:
            raise ValueError("Use a positive quantity with two decimal places")
        return value

    @field_validator("request_key")
    @classmethod
    def canonical_key(cls, value: str) -> str:
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Use a canonical request UUID") from exc
        return value


def _hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _money(value: Decimal) -> str:
    return f"{Decimal(value):.2f}"


def _expected_reservation_key(organization_id: int, demand_id: int, allocation_key: str) -> str:
    """Derive a stable idempotency key for the reserve created by an allocation."""
    return str(uuid5(NAMESPACE_URL, f"crm-erp:deal-demand-allocation:{organization_id}:{demand_id}:{allocation_key}"))


async def _deal_facts(session: AsyncSession, org_id: int, deal_id: int, data: DemandCreate):
    owner = await session.scalar(
        select(DealOwnership)
        .where(DealOwnership.organization_id == org_id, DealOwnership.deal_id == deal_id)
        .with_for_update()
    )
    deal = await session.scalar(select(Deal).where(Deal.id == deal_id).with_for_update())
    item = await session.scalar(
        select(DealItem).where(DealItem.id == data.deal_item_id, DealItem.deal_id == deal_id).with_for_update()
    )
    sku = await session.get(Sku, item.sku_id) if item is not None else None
    if owner is None or deal is None or item is None or sku is None:
        raise HTTPException(409, "Confirm the organization, CRM deal, deal item and SKU")
    if deal.stage in {"lost", "rp_lost", "tn_lost"}:
        raise HTTPException(409, "A lost deal cannot create a procurement demand")
    if item.qty <= 0 or not sku.is_active:
        raise HTTPException(409, "The deal item must reference an active SKU and a positive quantity")
    if data.document_id is not None:
        document = await session.get(DealDocument, data.document_id)
        if document is None or document.deal_id != deal_id or document.kind != "invoice":
            raise HTTPException(409, "The source document must be an invoice of the CRM deal")
    return deal, item, sku


async def _demand_projection(session: AsyncSession, org_id: int, demand: DealProcurementDemand) -> dict:
    allocations = (
        await session.scalars(
            select(DealProcurementAllocation)
            .where(
                DealProcurementAllocation.organization_id == org_id,
                DealProcurementAllocation.demand_id == demand.id,
            )
            .order_by(DealProcurementAllocation.id)
        )
    ).all()
    released_by_allocation: dict[int, Decimal] = {}
    for row in allocations:
        reservation_key = _expected_reservation_key(org_id, demand.id, row.request_key)
        reservation_id = await session.scalar(
            select(ExpectedReservation.id).where(
                ExpectedReservation.organization_id == org_id,
                ExpectedReservation.request_key == reservation_key,
            )
        )
        released_by_allocation[row.id] = Decimal(
            await session.scalar(
                select(func.coalesce(func.sum(ExpectedReservationEvent.qty), 0)).where(
                    ExpectedReservationEvent.organization_id == org_id,
                    ExpectedReservationEvent.reservation_id == reservation_id,
                    ExpectedReservationEvent.kind == "release",
                )
            )
            if reservation_id is not None
            else 0
        )
    allocated = sum(
        (max(row.qty - released_by_allocation[row.id], Decimal("0")) for row in allocations),
        Decimal("0"),
    )
    remaining = max(Decimal(demand.qty) - allocated, Decimal("0"))
    return {
        "id": demand.id,
        "organization_id": org_id,
        "deal_id": demand.deal_id,
        "deal_item_id": demand.deal_item_id,
        "sku_id": demand.sku_id,
        "sku_code": demand.sku_code,
        "qty": _money(demand.qty),
        "ordered_qty": _money(allocated),
        "free_qty": _money(remaining),
        "document_id": demand.document_id,
        "request_key": demand.request_key,
        "allocations": [
            {
                "id": row.id,
                "order_id": row.order_id,
                "order_line_id": row.order_line_id,
                "qty": _money(row.qty),
            }
            for row in allocations
        ],
    }


async def _create_demand(session: AsyncSession, org_id: int, actor: str, deal_id: int, data: DemandCreate):
    _deal, item, sku = await _deal_facts(session, org_id, deal_id, data)
    command = {"deal_id": deal_id, **data.model_dump(mode="json")}
    request_hash = _hash(command)
    existing = await session.scalar(
        select(DealProcurementDemand)
        .where(
            DealProcurementDemand.organization_id == org_id,
            DealProcurementDemand.request_key == data.request_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Demand request key identifies another command")
        return await _demand_projection(session, org_id, existing), True
    used = await session.scalar(
        select(func.coalesce(func.sum(DealProcurementDemand.qty), 0)).where(
            DealProcurementDemand.organization_id == org_id,
            DealProcurementDemand.deal_item_id == item.id,
        )
    )
    if Decimal(data.qty) > max(Decimal(item.qty) - Decimal(used or 0), Decimal("0")):
        raise HTTPException(409, "Demand exceeds the quantity on the CRM deal item")
    row = DealProcurementDemand(
        organization_id=org_id,
        deal_id=deal_id,
        deal_item_id=item.id,
        sku_id=sku.id,
        sku_code=sku.code,
        qty=Decimal(data.qty),
        document_id=data.document_id,
        request_key=data.request_key,
        request_hash=request_hash,
        evidence=data.evidence,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return await _demand_projection(session, org_id, row), False


async def _allocate(session: AsyncSession, org_id: int, actor: str, demand_id: int, data: AllocationCreate):
    demand = await session.scalar(
        select(DealProcurementDemand)
        .where(DealProcurementDemand.id == demand_id, DealProcurementDemand.organization_id == org_id)
        .with_for_update()
    )
    if demand is None:
        raise HTTPException(404, "Procurement demand not found")
    command = {"demand_id": demand_id, **data.model_dump(mode="json")}
    request_hash = _hash(command)
    existing = await session.scalar(
        select(DealProcurementAllocation)
        .where(
            DealProcurementAllocation.organization_id == org_id,
            DealProcurementAllocation.request_key == data.request_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Allocation request key identifies another command")
        return await _demand_projection(session, org_id, demand), True
    deal = await session.scalar(select(Deal).where(Deal.id == demand.deal_id).with_for_update())
    if deal is None or deal.stage == "lost" or deal.stage.endswith("_lost"):
        raise HTTPException(409, "A lost deal cannot receive a new procurement allocation")
    owner = await session.scalar(
        select(PurchaseOwnership).where(
            PurchaseOwnership.organization_id == org_id,
            PurchaseOwnership.kind == "order",
            PurchaseOwnership.source_id == data.order_id,
        )
    )
    order = await session.scalar(select(PurchaseOrder).where(PurchaseOrder.id == data.order_id).with_for_update())
    line = await session.scalar(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.id == data.order_line_id, PurchaseOrderLine.order_id == data.order_id)
        .with_for_update()
    )
    if owner is None or order is None or line is None:
        raise HTTPException(409, "Confirm the organization, supplier order and exact order line")
    if order.status not in OPEN_ORDER_STATUSES:
        raise HTTPException(409, "Demand allocation requires an open supplier order")
    if line.sku_code != demand.sku_code:
        raise HTTPException(409, "Supplier order line must match the demand SKU")
    demand_projection = await _demand_projection(session, org_id, demand)
    if Decimal(data.qty) > Decimal(demand_projection["free_qty"]):
        raise HTTPException(409, "Demand quantity is already allocated")
    # Allocation is the point at which the customer's part becomes a preliminary
    # reservation.  Accepted quantities and earlier manually-created expected
    # reservations therefore consume the same free expected quantity.
    expected_view = await expected_projection(session, org_id, line)
    if Decimal(data.qty) > Decimal(expected_view["free_expected"]):
        raise HTTPException(409, "The supplier order line has no free expected quantity")
    reservation_key = _expected_reservation_key(org_id, demand.id, data.request_key)
    reservation_hash = _hash({
        "order_id": data.order_id,
        "order_line_id": data.order_line_id,
        "deal_id": demand.deal_id,
        "document_id": demand.document_id,
        "qty": _money(Decimal(data.qty)),
        "request_key": reservation_key,
        "evidence": data.evidence,
    })
    existing_reservation = await session.scalar(
        select(ExpectedReservation)
        .where(
            ExpectedReservation.organization_id == org_id,
            ExpectedReservation.request_key == reservation_key,
        )
        .with_for_update()
    )
    if existing_reservation is not None:
        raise HTTPException(409, "Allocation already has an incompatible expected reservation")
    allocation = DealProcurementAllocation(
        organization_id=org_id,
        demand_id=demand.id,
        order_id=order.id,
        order_line_id=line.id,
        sku_code=line.sku_code,
        qty=Decimal(data.qty),
        request_key=data.request_key,
        request_hash=request_hash,
        evidence=data.evidence,
        actor=actor,
    )
    session.add(allocation)
    session.add(
        ExpectedReservation(
            organization_id=org_id,
            order_id=order.id,
            order_line_id=line.id,
            deal_id=demand.deal_id,
            demand_id=demand.id,
            document_id=demand.document_id,
            sku_code=line.sku_code,
            qty=Decimal(data.qty),
            request_key=reservation_key,
            request_hash=reservation_hash,
            evidence=data.evidence,
            actor=actor,
        )
    )
    await session.flush()
    return await _demand_projection(session, org_id, demand), False


async def _deal_demand_list(session: AsyncSession, org_id: int, deal_id: int):
    if await session.scalar(
        select(DealOwnership.deal_id).where(
            DealOwnership.organization_id == org_id, DealOwnership.deal_id == deal_id
        )
    ) is None:
        raise HTTPException(404, "CRM deal is not assigned to this organization")
    rows = (
        await session.scalars(
            select(DealProcurementDemand)
            .where(
                DealProcurementDemand.organization_id == org_id,
                DealProcurementDemand.deal_id == deal_id,
            )
            .order_by(DealProcurementDemand.id)
        )
    ).all()
    return [await _demand_projection(session, org_id, row) for row in rows]


async def _order_demand_projection(session: AsyncSession, org_id: int, order_id: int):
    if await session.scalar(
        select(PurchaseOwnership.source_id).where(
            PurchaseOwnership.organization_id == org_id,
            PurchaseOwnership.kind == "order",
            PurchaseOwnership.source_id == order_id,
        )
    ) is None:
        raise HTTPException(404, "Supplier order is not assigned to this organization")
    lines = (
        await session.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order_id).order_by(PurchaseOrderLine.id)
        )
    ).all()
    result = []
    for line in lines:
        allocations = (
            await session.scalars(
                select(DealProcurementAllocation)
                .where(
                    DealProcurementAllocation.organization_id == org_id,
                    DealProcurementAllocation.order_line_id == line.id,
                )
                .order_by(DealProcurementAllocation.id)
            )
        ).all()
        candidates = []
        demand_rows = (
            await session.scalars(
                select(DealProcurementDemand)
                .where(
                    DealProcurementDemand.organization_id == org_id,
                    DealProcurementDemand.sku_code == line.sku_code,
                )
                .order_by(DealProcurementDemand.id)
            )
        ).all()
        for demand in demand_rows:
            deal = await session.scalar(select(Deal).where(Deal.id == demand.deal_id))
            if deal is None or deal.stage == "lost" or deal.stage.endswith("_lost"):
                continue
            projection = await _demand_projection(session, org_id, demand)
            if projection["free_qty"] == "0.00":
                continue
            candidates.append({
                "demand_id": demand.id,
                "deal_id": demand.deal_id,
                "deal_item_id": demand.deal_item_id,
                "sku_code": demand.sku_code,
                "qty": projection["qty"],
                "free_qty": projection["free_qty"],
                "document_id": demand.document_id,
            })
        allocated = Decimal("0")
        for row in allocations:
            reservation_key = _expected_reservation_key(org_id, row.demand_id, row.request_key)
            reservation_id = await session.scalar(
                select(ExpectedReservation.id).where(
                    ExpectedReservation.organization_id == org_id,
                    ExpectedReservation.request_key == reservation_key,
                )
            )
            released = Decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(ExpectedReservationEvent.qty), 0)).where(
                        ExpectedReservationEvent.organization_id == org_id,
                        ExpectedReservationEvent.reservation_id == reservation_id,
                        ExpectedReservationEvent.kind == "release",
                    )
                )
                if reservation_id is not None
                else 0
            )
            allocated += max(row.qty - released, Decimal("0"))
        result.append(
            {
                "order_line_id": line.id,
                "sku_code": line.sku_code,
                "ordered": _money(line.qty),
                "client_ordered": _money(allocated),
                "free_for_client": _money(max(Decimal(line.qty) - allocated, Decimal("0"))),
                "candidates": candidates,
                "allocations": [
                    {"id": row.id, "demand_id": row.demand_id, "qty": _money(row.qty)}
                    for row in allocations
                ],
            }
        )
    return {"organization_id": org_id, "order_id": order_id, "lines": result}


router = APIRouter(tags=["Потребности закупок из CRM"])


@router.post("/organizations/{org_id}/deals/{deal_id}/demands", status_code=201)
async def create_demand_route(org_id: int, deal_id: int, data: DemandCreate, ctx=Depends(scoped)):
    session, actor = ctx
    try:
        result, replayed = await _create_demand(session, org_id, actor, deal_id, data)
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Concurrent or duplicate procurement demand command") from exc
    return {"replayed": replayed, **result}


@router.get("/organizations/{org_id}/deals/{deal_id}/demands")
async def list_demand_route(org_id: int, deal_id: int, ctx=Depends(scoped)):
    session, _actor = ctx
    result = await _deal_demand_list(session, org_id, deal_id)
    await session.rollback()
    return {"organization_id": org_id, "deal_id": deal_id, "demands": result}


@router.post("/organizations/{org_id}/demands/{demand_id}/allocations", status_code=201)
async def allocate_demand_route(org_id: int, demand_id: int, data: AllocationCreate, ctx=Depends(scoped)):
    session, actor = ctx
    try:
        result, replayed = await _allocate(session, org_id, actor, demand_id, data)
        allocation = await session.scalar(
            select(DealProcurementAllocation).where(
                DealProcurementAllocation.organization_id == org_id,
                DealProcurementAllocation.request_key == data.request_key,
            )
        )
        if allocation is None:
            raise HTTPException(500, "Allocation receipt is unavailable")
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Concurrent or duplicate demand allocation command") from exc
    return {
        "replayed": replayed,
        "allocation_request_key": allocation.request_key,
        "allocation": {
            "id": allocation.id,
            "demand_id": allocation.demand_id,
            "order_id": allocation.order_id,
            "order_line_id": allocation.order_line_id,
            "sku_code": allocation.sku_code,
            "qty": _money(allocation.qty),
        },
        **result,
    }


@router.get("/organizations/{org_id}/orders/{order_id}/deal-demands")
async def order_demand_route(org_id: int, order_id: int, ctx=Depends(scoped)):
    session, _actor = ctx
    result = await _order_demand_projection(session, org_id, order_id)
    await session.rollback()
    return result
