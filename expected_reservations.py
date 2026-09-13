"""Preliminary reservations against quantities ordered from suppliers.

They are an expected-stock register only: no warehouse movement is created here.
"""
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.procurement.models import OPEN_ORDER_STATUSES, PurchaseOrder, PurchaseOrderLine
from modules.procurement.ownership import PurchaseOwnership
from modules.procurement.receipt_documents import ReceiptDocument, ReceiptRevision, scoped
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.models import Deal, DealDocument


class ExpectedReservation(Base):
    __tablename__ = "expected_reservation"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key", name="expected_reservation_request"),
        CheckConstraint("qty > 0 AND qty < 1000000000000", name="expected_reservation_qty"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_line_id: Mapped[int] = mapped_column(Integer, index=True)
    deal_id: Mapped[int] = mapped_column(Integer, index=True)
    demand_id: Mapped[int | None] = mapped_column(Integer, index=True)
    document_id: Mapped[int | None] = mapped_column(Integer)
    sku_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExpectedReservationEvent(Base):
    __tablename__ = "expected_reservation_event"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key", name="expected_reservation_event_request"),
        CheckConstraint("kind IN ('release', 'convert') AND qty > 0 AND qty < 1000000000000", name="expected_reservation_event_values"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("procurement.expected_reservation.id"))
    kind: Mapped[str] = mapped_column(String(16))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    request_key: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExpectedConversionRequest(Base):
    """Durable event snapshot; a pending request is reconsidered after QC."""

    __tablename__ = "expected_conversion_request"
    __table_args__ = (
        UniqueConstraint("organization_id", "event_identity", name="expected_conversion_request_event"),
        UniqueConstraint("organization_id", "reservation_identity", name="expected_conversion_request_reservation"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    event_identity: Mapped[str] = mapped_column(String(64))
    reservation_identity: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    reservation_ids: Mapped[list] = mapped_column(JSON)
    completed: Mapped[bool] = mapped_column(default=False)


class PhysicalReceiptAcceptance(Base):
    """Immutable QC evidence received from a bound WMS primary receipt."""

    __tablename__ = "physical_receipt_acceptance"
    __table_args__ = (
        UniqueConstraint("organization_id", "event_id", name="physical_receipt_acceptance_event"),
        UniqueConstraint("organization_id", "receipt_id", name="physical_receipt_acceptance_receipt"),
        CheckConstraint("organization_id > 0 AND event_id > 0 AND receipt_id > 0", name="physical_receipt_acceptance_refs"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    receipt_id: Mapped[int] = mapped_column(Integer, index=True)
    source_receipt_id: Mapped[int] = mapped_column(Integer, index=True)
    source_version: Mapped[int] = mapped_column(Integer)
    lines: Mapped[list] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(*_args):
    raise ValueError("Expected reservation history is immutable; append an event")


event.listen(ExpectedReservation, "before_update", immutable)
event.listen(ExpectedReservation, "before_delete", immutable)
event.listen(ExpectedReservationEvent, "before_update", immutable)
event.listen(ExpectedReservationEvent, "before_delete", immutable)
event.listen(PhysicalReceiptAcceptance, "before_update", immutable)
event.listen(PhysicalReceiptAcceptance, "before_delete", immutable)


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_id: int = Field(gt=0, strict=True)
    order_line_id: int = Field(gt=0, strict=True)
    deal_id: int = Field(gt=0, strict=True)
    document_id: int | None = Field(default=None, gt=0, strict=True)
    qty: str = Field(strict=True)
    request_key: str = Field(strict=True, min_length=8, max_length=128)
    evidence: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("qty")
    @classmethod
    def exact_qty(cls, value):
        if not re.fullmatch(r"(?:0|[1-9]\d{0,11})\.\d{2}", value) or Decimal(value) <= 0:
            raise ValueError("Use a positive quantity with two decimal places")
        return value

    @field_validator("request_key")
    @classmethod
    def canonical_key(cls, value):
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Use a canonical request UUID") from exc
        return value


class ReservationEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["release", "convert"]
    qty: str = Field(strict=True)
    request_key: str = Field(strict=True, min_length=8, max_length=128)
    evidence: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("qty")
    @classmethod
    def exact_qty(cls, value):
        if not re.fullmatch(r"(?:0|[1-9]\d{0,11})\.\d{2}", value) or Decimal(value) <= 0:
            raise ValueError("Use a positive quantity with two decimal places")
        return value

    @field_validator("request_key")
    @classmethod
    def canonical_key(cls, value):
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Use a canonical request UUID") from exc
        return value


def _hash(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _accepted(session: AsyncSession, org_id: int, line_id: int) -> Decimal:
    rows = (await session.execute(select(ReceiptDocument, ReceiptRevision).join(
        ReceiptRevision, (ReceiptRevision.receipt_id == ReceiptDocument.id)
        & (ReceiptRevision.version == ReceiptDocument.current_version),
    ).where(ReceiptDocument.organization_id == org_id))).all()
    total = Decimal("0")
    for _document, revision in rows:
        for item in revision.document.get("items", []):
            if item.get("order_line_id") == line_id:
                total += Decimal(str(item["quantity"]))
    return total


async def _physical_accepted(session: AsyncSession, org_id: int, line_id: int) -> Decimal:
    rows = (await session.scalars(select(PhysicalReceiptAcceptance).where(
        PhysicalReceiptAcceptance.organization_id == org_id,
    ))).all()
    total = Decimal("0")
    for row in rows:
        for item in row.lines or []:
            if item.get("order_line_id") == line_id:
                total += Decimal(str(item["accepted_qty"]))
    return total


async def _facts(session, org_id: int, data: ReservationCreate):
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == "order",
        PurchaseOwnership.source_id == data.order_id).with_for_update())
    order = await session.scalar(select(PurchaseOrder).where(PurchaseOrder.id == data.order_id).with_for_update())
    line = await session.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.id == data.order_line_id, PurchaseOrderLine.order_id == data.order_id).with_for_update())
    deal_owner = await session.scalar(select(DealOwnership).where(
        DealOwnership.organization_id == org_id, DealOwnership.deal_id == data.deal_id))
    deal = await session.get(Deal, data.deal_id)
    if owner is None or order is None or line is None or deal_owner is None or deal is None:
        raise HTTPException(409, "Confirm the organization, supplier order line and CRM deal")
    if order.status not in OPEN_ORDER_STATUSES:
        raise HTTPException(409, "Expected reservation requires an open supplier order")
    if data.document_id is not None:
        doc = await session.get(DealDocument, data.document_id)
        if doc is None or doc.deal_id != data.deal_id or doc.kind != "invoice":
            raise HTTPException(409, "Invoice does not belong to the CRM deal")
    return order, line


async def _projection(session, org_id: int, line: PurchaseOrderLine):
    rows = (await session.scalars(select(ExpectedReservation).where(
        ExpectedReservation.organization_id == org_id, ExpectedReservation.order_line_id == line.id
    ).order_by(ExpectedReservation.id))).all()
    events = (await session.scalars(select(ExpectedReservationEvent).where(
        ExpectedReservationEvent.organization_id == org_id,
        ExpectedReservationEvent.reservation_id.in_([r.id for r in rows] or [-1])))).all()
    accepted = await _accepted(session, org_id, line.id)
    warehouse_accepted = await _physical_accepted(session, org_id, line.id)
    allocated = sum((r.qty for r in rows), Decimal("0"))
    released = sum((e.qty for e in events if e.kind == "release"), Decimal("0"))
    converted = sum((e.qty for e in events if e.kind == "convert"), Decimal("0"))
    expected = max(line.qty - accepted, Decimal("0"))
    pending = allocated - released - converted
    pending_requests = (await session.scalars(select(ExpectedConversionRequest).where(
        ExpectedConversionRequest.organization_id == org_id,
        ExpectedConversionRequest.completed.is_(False),
    ))).all()
    reservation_ids = {row.id for row in rows}
    pending_conversion_count = sum(
        bool(reservation_ids.intersection(request.reservation_ids)) for request in pending_requests
    )
    # This is a read-only reconciliation aid.  It is based on accepted source
    # quantities and conversion events; it does not create a WMS reservation
    # and therefore never authorizes shipment by itself.
    conversion_capacity = max(accepted - converted, Decimal("0"))
    physical_conversion_capacity = max(warehouse_accepted - converted, Decimal("0"))
    reservation_view = []
    for row in rows:
        released_qty = sum((e.qty for e in events if e.reservation_id == row.id and e.kind == "release"), Decimal("0"))
        converted_qty = sum((e.qty for e in events if e.reservation_id == row.id and e.kind == "convert"), Decimal("0"))
        remaining = max(row.qty - released_qty - converted_qty, Decimal("0"))
        convertible = min(remaining, conversion_capacity)
        conversion_capacity -= convertible
        physical_convertible = min(remaining, physical_conversion_capacity)
        physical_conversion_capacity -= physical_convertible
        reservation_view.append({"id": row.id, "deal_id": row.deal_id, "demand_id": row.demand_id, "document_id": row.document_id,
                                 "qty": f"{row.qty:.2f}", "released": f"{released_qty:.2f}",
                                 "converted": f"{converted_qty:.2f}", "convertible": f"{convertible:.2f}",
                                 "physical_convertible": f"{physical_convertible:.2f}"})
    return {"order_line_id": line.id, "sku_code": line.sku_code,
            "pending_conversion_count": pending_conversion_count,
            "ordered": f"{line.qty:.2f}", "accepted": f"{accepted:.2f}",
            "warehouse_accepted": f"{warehouse_accepted:.2f}", "expected": f"{expected:.2f}",
            "converted": f"{converted:.2f}", "convertible": f"{max(accepted - converted, Decimal(0)):.2f}",
            "physical_convertible": f"{max(warehouse_accepted - converted, Decimal(0)):.2f}",
            "expected_reserved": f"{pending:.2f}", "free_expected": f"{max(expected - pending, Decimal(0)):.2f}",
            "uncovered": f"{max(pending - expected, Decimal(0)):.2f}",
            "reservations": reservation_view}


async def create_expected(session, org_id, actor, data: ReservationCreate):
    _order, line = await _facts(session, org_id, data)
    body = data.model_dump(mode="json")
    request_hash = _hash(body)
    existing = await session.scalar(select(ExpectedReservation).where(
        ExpectedReservation.organization_id == org_id, ExpectedReservation.request_key == data.request_key).with_for_update())
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Expected reservation key identifies another command")
        return await _projection(session, org_id, line), True
    projection = await _projection(session, org_id, line)
    if Decimal(data.qty) > Decimal(projection["free_expected"]):
        raise HTTPException(409, "Free expected quantity is insufficient")
    row = ExpectedReservation(organization_id=org_id, order_id=data.order_id, order_line_id=data.order_line_id,
        deal_id=data.deal_id, document_id=data.document_id, sku_code=line.sku_code, qty=Decimal(data.qty),
        request_key=data.request_key, request_hash=request_hash, evidence=data.evidence, actor=actor)
    session.add(row)
    await session.flush()
    return await _projection(session, org_id, line), False


async def append_event(session, org_id, actor, reservation_id, data: ReservationEventInput):
    from modules.accounting.models import Organization

    # Match the lock order of QC/request reconciliation before locking reserves.
    await session.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    row = await session.scalar(select(ExpectedReservation).where(
        ExpectedReservation.id == reservation_id, ExpectedReservation.organization_id == org_id).with_for_update())
    if row is None:
        raise HTTPException(404, "Expected reservation not found")
    existing = await session.scalar(select(ExpectedReservationEvent).where(
        ExpectedReservationEvent.organization_id == org_id, ExpectedReservationEvent.request_key == data.request_key).with_for_update())
    if existing:
        if existing.reservation_id != reservation_id or existing.kind != data.kind or existing.qty != Decimal(data.qty):
            raise HTTPException(409, "Expected reservation event key identifies another command")
        line = await session.get(PurchaseOrderLine, row.order_line_id)
        return await _projection(session, org_id, line), True
    line = await session.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.id == row.order_line_id).with_for_update())
    projection = await _projection(session, org_id, line)
    current = next(r for r in projection["reservations"] if r["id"] == row.id)
    remaining = Decimal(current["qty"]) - Decimal(current["released"]) - Decimal(current["converted"])
    if Decimal(data.qty) > remaining:
        raise HTTPException(409, "Event exceeds this client reservation")
    if data.kind == "convert":
        # Conversion capacity is allocated FIFO by immutable reservation id in
        # `_projection`.  Enforce the same order at write time; otherwise a
        # later client could consume accepted stock while an earlier reserve
        # still reports it as convertible.
        if Decimal(data.qty) > Decimal(current["physical_convertible"]):
            raise HTTPException(409, "Physical conversion exceeds the deterministically available quantity")
    session.add(ExpectedReservationEvent(organization_id=org_id, reservation_id=row.id, kind=data.kind,
        qty=Decimal(data.qty), request_key=data.request_key, evidence=data.evidence, actor=actor))
    await session.flush()
    if data.kind == "release":
        from modules.procurement.events import _reconcile_expected_requests

        await _reconcile_expected_requests(SimpleNamespace(session=session), org_id)
    return await _projection(session, org_id, line), False


router = APIRouter(tags=["Ожидаемые резервы закупок"])


@router.post("/organizations/{org_id}/expected-reservations", status_code=201)
async def create_route(org_id: int, data: ReservationCreate, ctx=Depends(scoped)):
    session, actor = ctx
    try:
        result, replayed = await create_expected(session, org_id, actor, data)
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    return {"replayed": replayed, **result}


@router.post("/organizations/{org_id}/expected-reservations/{reservation_id}/events", status_code=201)
async def event_route(org_id: int, reservation_id: int, data: ReservationEventInput, ctx=Depends(scoped)):
    session, actor = ctx
    try:
        result, replayed = await append_event(session, org_id, actor, reservation_id, data)
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    return {"replayed": replayed, **result}


@router.get("/organizations/{org_id}/expected-reservations/{order_line_id}")
async def projection_route(org_id: int, order_line_id: int, ctx=Depends(scoped)):
    session, _actor = ctx
    line = await session.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.id == order_line_id))
    if line is None:
        raise HTTPException(404, "Order line not found")
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == "order",
        PurchaseOwnership.source_id == line.order_id))
    if owner is None:
        raise HTTPException(404, "Order line not found")
    result = await _projection(session, org_id, line)
    await session.rollback()
    return result


@router.get("/organizations/{org_id}/expected-reservations/order/{order_id}")
async def order_projection_route(org_id: int, order_id: int, ctx=Depends(scoped)):
    session, _actor = ctx
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == "order",
        PurchaseOwnership.source_id == order_id))
    if owner is None:
        raise HTTPException(404, "Supplier order not found")
    lines = (await session.scalars(select(PurchaseOrderLine).where(
        PurchaseOrderLine.order_id == order_id).order_by(PurchaseOrderLine.id))).all()
    result = {"organization_id": org_id, "order_id": order_id,
              "lines": [await _projection(session, org_id, line) for line in lines]}
    await session.rollback()
    return result
