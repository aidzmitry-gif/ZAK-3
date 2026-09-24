"""Request-owned RFQ workflow. Unmatched legacy RFQs remain outside company books."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BeforeValidator, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, event, func, select
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.core import Core
from core.runtime.deps import get_core
from modules.procurement.models import PurchaseRequest, Rfq, RfqBid, Supplier
from modules.procurement.ownership import PurchaseOwnership, current_read_scope
from modules.procurement.receipt_documents import Input, exact, immutable
from modules.procurement.routes import _bids_of, _rfq_out
from modules.procurement.supplier_identity import active_bound_suppliers


router = APIRouter(tags=["RFQ по юрлицу"])
Quote = Annotated[Decimal, BeforeValidator(exact), Field(ge=0, max_digits=14, decimal_places=2)]


class RfqHistory(Base):
    __tablename__ = "rfq_history"
    __table_args__ = {"schema": "procurement"}

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("procurement.rfq.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    before_state: Mapped[dict | None] = mapped_column(JSON)
    after_state: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(RfqHistory, "before_update", immutable)
event.listen(RfqHistory, "before_delete", immutable)


class Create(Input):
    request_key: UUID
    request_id: int = Field(gt=0, strict=True)


class Bid(Input):
    bid_key: UUID
    supplier_id: int = Field(gt=0, strict=True)
    price_byn: Quote
    lead_time_days: int | None = Field(default=None, ge=0, strict=True)
    incoterms: str = Field(default="", max_length=16)
    note: str = Field(default="", max_length=400)


class Award(Input):
    bid_id: int = Field(gt=0, strict=True)


def output(org_id: int, rfq: Rfq, bids: list[RfqBid]):
    return {**_rfq_out(rfq, bids).model_dump(mode="json"), "organization_id": org_id,
            "request_key": rfq.request_key, "number": f"RFQ-{rfq.id:06d}"}


async def owned(session, org_id: int, rfq_id: int, *, lock: bool = False) -> Rfq:
    statement = (select(Rfq).join(PurchaseOwnership,
        (PurchaseOwnership.kind == "request") & (PurchaseOwnership.source_id == Rfq.request_id))
        .where(Rfq.id == rfq_id, Rfq.request_key.is_not(None),
               PurchaseOwnership.organization_id == org_id))
    if lock:
        statement = statement.with_for_update(of=Rfq)
    rfq = await session.scalar(statement)
    if rfq is None:
        raise HTTPException(404, "RFQ not found in this organization")
    return rfq


async def checked_supplier(session, supplier_id: int):
    supplier = await session.scalar(active_bound_suppliers().where(
        Supplier.id == supplier_id).with_for_update(read=True))
    if supplier is None:
        raise HTTPException(409, "Choose an active supplier linked to the counterparty directory")
    return supplier


async def history(session, org_id: int, rfq_id: int):
    rows = (await session.scalars(select(RfqHistory).where(
        RfqHistory.organization_id == org_id, RfqHistory.rfq_id == rfq_id,
    ).order_by(RfqHistory.id))).all()
    return [{"id": row.id, "action": row.action, "before": row.before_state,
             "after": row.after_state, "actor": row.actor, "created_at": row.created_at} for row in rows]


@router.get("/organizations/{org_id}/rfq")
async def list_owned(org_id: int, after_id: int = Query(0, ge=0), ctx=Depends(current_read_scope)):
    session, _ = ctx
    rows = (await session.scalars(select(Rfq).join(PurchaseOwnership,
        (PurchaseOwnership.kind == "request") & (PurchaseOwnership.source_id == Rfq.request_id))
        .where(PurchaseOwnership.organization_id == org_id, Rfq.request_key.is_not(None),
               Rfq.id > after_id)
        .order_by(Rfq.id).limit(51))).all()
    page = rows[:50]
    ids = [row.id for row in page]
    bids: dict[int, list[RfqBid]] = {}
    if ids:
        for bid in (await session.scalars(select(RfqBid).where(RfqBid.rfq_id.in_(ids)))).all():
            bids.setdefault(bid.rfq_id, []).append(bid)
    return {"organization_id": org_id, "items": [output(org_id, row, bids.get(row.id, [])) for row in page],
            "next_after_id": page[-1].id if len(rows) > 50 else None}


@router.post("/organizations/{org_id}/rfq", status_code=201)
async def create(org_id: int, data: Create, ctx=Depends(current_read_scope)):
    session, actor = ctx
    key = str(data.request_key)
    existing = await session.scalar(select(Rfq).where(Rfq.request_key == key))
    if existing is not None:
        if existing.request_id != data.request_id:
            raise HTTPException(409, "RFQ key belongs to another request")
        await owned(session, org_id, existing.id)
        return output(org_id, existing, await _bids_of(session, existing.id))
    request = await session.scalar(select(PurchaseRequest).join(PurchaseOwnership,
        (PurchaseOwnership.kind == "request") & (PurchaseOwnership.source_id == PurchaseRequest.id))
        .where(PurchaseRequest.id == data.request_id, PurchaseOwnership.organization_id == org_id))
    if (request is None or request.stage not in ("need", "sourcing", "nego", "analysis")
            or not request.item.strip() or request.qty <= 0):
        raise HTTPException(409, "Choose a valid request owned by this organization")
    rfq = Rfq(request_key=key, request_id=request.id, item=request.item,
              qty=Decimal(request.qty), sku_code="", status="open")
    session.add(rfq)
    await session.flush()
    session.add(RfqHistory(organization_id=org_id, rfq_id=rfq.id, action="create",
        before_state=None, after_state={"request_id": request.id, "item": request.item,
            "quantity": str(rfq.qty), "status": "open"}, actor=actor))
    await session.flush()
    return output(org_id, rfq, [])


@router.get("/organizations/{org_id}/rfq/{rfq_id}")
async def detail(org_id: int, rfq_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    rfq = await owned(session, org_id, rfq_id)
    return output(org_id, rfq, await _bids_of(session, rfq.id))


@router.get("/organizations/{org_id}/rfq/{rfq_id}/history")
async def document_history(org_id: int, rfq_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    await owned(session, org_id, rfq_id)
    return {"organization_id": org_id, "rfq_id": rfq_id,
            "items": await history(session, org_id, rfq_id)}


@router.post("/organizations/{org_id}/rfq/{rfq_id}/bids", status_code=201)
async def add_bid(org_id: int, rfq_id: int, data: Bid, ctx=Depends(current_read_scope)):
    session, actor = ctx
    rfq = await owned(session, org_id, rfq_id, lock=True)
    key = str(data.bid_key)
    existing = await session.scalar(select(RfqBid).where(RfqBid.bid_key == key))
    if existing is not None:
        if (existing.rfq_id != rfq_id or existing.supplier_id != data.supplier_id
                or existing.price_byn != data.price_byn or existing.lead_time_days != data.lead_time_days
                or existing.incoterms != data.incoterms or existing.note != data.note):
            raise HTTPException(409, "Bid key belongs to another proposal")
        return output(org_id, rfq, await _bids_of(session, rfq_id))
    if rfq.status != "open":
        raise HTTPException(409, "RFQ is closed")
    await checked_supplier(session, data.supplier_id)
    proposal = RfqBid(bid_key=key, rfq_id=rfq_id, supplier_id=data.supplier_id,
        price_byn=data.price_byn, lead_time_days=data.lead_time_days,
        incoterms=data.incoterms, note=data.note)
    session.add(proposal)
    await session.flush()
    session.add(RfqHistory(organization_id=org_id, rfq_id=rfq_id, action="bid",
        before_state=None, after_state={"bid_id": proposal.id, "supplier_id": data.supplier_id,
            "price_byn": str(data.price_byn), "lead_time_days": data.lead_time_days,
            "incoterms": data.incoterms, "note": data.note}, actor=actor))
    await session.flush()
    return output(org_id, rfq, await _bids_of(session, rfq_id))


@router.post("/organizations/{org_id}/rfq/{rfq_id}/award")
async def award(org_id: int, rfq_id: int, data: Award, core: Core = Depends(get_core),
                ctx=Depends(current_read_scope)):
    session, actor = ctx
    rfq = await owned(session, org_id, rfq_id, lock=True)
    bids = await _bids_of(session, rfq_id)
    winner = next((bid for bid in bids if bid.id == data.bid_id), None)
    if winner is None:
        raise HTTPException(404, "Bid not found in this RFQ")
    if rfq.status == "awarded" and winner.is_winner:
        return output(org_id, rfq, bids)
    if rfq.status != "open":
        raise HTTPException(409, "RFQ is closed")
    await checked_supplier(session, winner.supplier_id)
    for bid in bids:
        bid.is_winner = bid.id == winner.id
    rfq.status = "awarded"
    session.add(RfqHistory(organization_id=org_id, rfq_id=rfq_id, action="award",
        before_state={"status": "open", "winner_bid_id": None},
        after_state={"status": "awarded", "winner_bid_id": winner.id}, actor=actor))
    core.event_bus.emit(session, "procurement.rfq.awarded", {
        "organization_id": org_id, "rfq_id": rfq.id, "request_id": rfq.request_id,
        "supplier_id": winner.supplier_id, "price_byn": str(winner.price_byn),
        "sku_code": rfq.sku_code, "entity_ref": f"rfq:{rfq.id}"})
    await session.flush()
    return output(org_id, rfq, bids)
