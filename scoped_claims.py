"""Company-owned supplier claims; historical production scraps stay unassigned."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, func, select
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.deps import get_core
from core.runtime.core import Core
from modules.procurement.models import PurchaseOrder, SupplierClaim
from modules.procurement.ownership import PurchaseOwnership, current_read_scope, plan_writer
from modules.procurement.receipt_documents import Input, immutable
from modules.procurement.schemas import SupplierClaimOut
from modules.procurement.supplier_identity import selected_supplier

router = APIRouter(tags=["Organization supplier claims"])


class ClaimOwnership(Base):
    __tablename__ = "claim_ownership"
    __table_args__ = (UniqueConstraint("claim_id", name="uq_claim_ownership_claim"),
                      UniqueConstraint("claim_key", name="uq_claim_ownership_key"),
                      {"schema": "procurement"})

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("procurement.supplier_claim.id"))
    claim_key: Mapped[str] = mapped_column(String(36))
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_order.id"))
    command_hash: Mapped[str] = mapped_column(String(64))
    command: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimEdit(Base):
    __tablename__ = "claim_edit"
    __table_args__ = (UniqueConstraint("edit_key", name="uq_claim_edit_key"),
                      {"schema": "procurement"})

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("procurement.supplier_claim.id"))
    edit_key: Mapped[str] = mapped_column(String(36))
    command_hash: Mapped[str] = mapped_column(String(64))
    command: Mapped[dict] = mapped_column(JSON)
    before_state: Mapped[dict] = mapped_column(JSON)
    after_state: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(ClaimOwnership, "before_update", immutable)
event.listen(ClaimOwnership, "before_delete", immutable)
event.listen(ClaimEdit, "before_update", immutable)
event.listen(ClaimEdit, "before_delete", immutable)


class ClaimCreate(Input):
    claim_key: UUID
    supplier_id: int = Field(gt=0, le=2147483647, strict=True)
    supplier: str = Field(min_length=1, max_length=255)
    supplier_unp: str = Field(max_length=32)
    order_id: int | None = Field(default=None, gt=0, le=2147483647, strict=True)
    item: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=400)
    claim_type: str = Field(min_length=1, max_length=32)
    qty_affected: int = Field(ge=0, le=2147483647, strict=True)
    amount_byn: str | None = Field(default=None, pattern=r"^(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?$")
    ownership_evidence: str = Field(min_length=1, max_length=1000)


class ClaimResolution(Input):
    edit_key: UUID
    expected_status: str = Field(pattern="^open$")
    status: str = Field(pattern="^(resolved|rejected)$")
    resolution: str = Field(min_length=1, max_length=500)


def command_hash(command: dict) -> str:
    raw = json.dumps(command, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_out(row: SupplierClaim, owner: ClaimOwnership) -> dict:
    created_at = owner.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return {"organization_id": owner.organization_id,
            "order_id": owner.order_id,
            "claim": SupplierClaimOut.model_validate(row).model_dump(mode="json"),
            "ownership": {"actor": owner.actor, "created_at": created_at.isoformat(),
                          "evidence": owner.evidence}}


@router.post("/organizations/{org_id}/claims", status_code=201)
async def create_claim(org_id: int, data: ClaimCreate, ctx=Depends(plan_writer)):
    session, actor = ctx
    command = data.model_dump(mode="json")
    digest = command_hash(command)
    key = str(data.claim_key)
    existing = await session.scalar(select(ClaimOwnership).where(ClaimOwnership.claim_key == key))
    if existing is not None:
        if existing.organization_id != org_id or existing.command_hash != digest or existing.command != command:
            raise HTTPException(409, "Claim key belongs to a different command")
        return existing.result

    supplier = await selected_supplier(session, data.supplier_id, data.supplier, data.supplier_unp)
    if supplier is None:
        raise HTTPException(409, "Select an active supplier from the counterparty directory")
    order = None
    if data.order_id is not None:
        order = await session.scalar(select(PurchaseOrder).join(PurchaseOwnership,
            (PurchaseOwnership.kind == "order") & (PurchaseOwnership.source_id == PurchaseOrder.id))
            .where(PurchaseOrder.id == data.order_id, PurchaseOwnership.organization_id == org_id)
            .with_for_update())
        if order is None or order.supplier_id != supplier.id:
            raise HTTPException(409, "Order does not belong to this company and supplier")

    claim = SupplierClaim(supplier_id=supplier.id, supplier=supplier.name, item=data.item,
                          reason=data.reason, order_code=order.number if order else "",
                          claim_type=data.claim_type, qty_affected=data.qty_affected,
                          amount_byn=Decimal(data.amount_byn) if data.amount_byn is not None else None,
                          status="open", source="manual", entity_ref=f"claim:{key}")
    session.add(claim)
    await session.flush()
    owner = ClaimOwnership(claim_id=claim.id, claim_key=key, organization_id=org_id,
                           order_id=order.id if order else None, command_hash=digest, command=command,
                           result={}, evidence=data.ownership_evidence, actor=actor,
                           created_at=datetime.now(timezone.utc))
    result = claim_out(claim, owner)
    owner.result = result
    session.add(owner)
    return result


@router.get("/organizations/{org_id}/claims")
async def list_claims(org_id: int, after_id: int = Query(default=0, ge=0),
                      ctx=Depends(current_read_scope)):
    session, _ = ctx
    rows = (await session.execute(select(SupplierClaim, ClaimOwnership).join(ClaimOwnership,
        ClaimOwnership.claim_id == SupplierClaim.id).where(
        ClaimOwnership.organization_id == org_id, SupplierClaim.id > after_id)
        .order_by(SupplierClaim.id).limit(51))).all()
    page = rows[:50]
    return {"organization_id": org_id, "items": [claim_out(claim, owner) for claim, owner in page],
            "next_after_id": page[-1][0].id if len(rows) > 50 else None,
            "unassigned_claims_excluded": True}


@router.get("/organizations/{org_id}/claims/{claim_id}")
async def get_claim(org_id: int, claim_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    row = (await session.execute(select(SupplierClaim, ClaimOwnership).join(ClaimOwnership,
        ClaimOwnership.claim_id == SupplierClaim.id).where(
        ClaimOwnership.organization_id == org_id, SupplierClaim.id == claim_id))).first()
    if row is None:
        raise HTTPException(404, "Claim not found for this company")
    return claim_out(*row)


@router.post("/organizations/{org_id}/claims/{claim_id}/resolve")
async def resolve_claim(org_id: int, claim_id: int, data: ClaimResolution,
                        ctx=Depends(plan_writer), core: Core = Depends(get_core)):
    session, actor = ctx
    command = data.model_dump(mode="json")
    digest = command_hash(command)
    existing = await session.scalar(select(ClaimEdit).where(ClaimEdit.edit_key == str(data.edit_key)))
    if existing is not None:
        if (existing.organization_id != org_id or existing.claim_id != claim_id
                or existing.command_hash != digest or existing.command != command):
            raise HTTPException(409, "Claim edit key belongs to a different command")
        return existing.result
    row = (await session.execute(select(SupplierClaim, ClaimOwnership).join(ClaimOwnership,
        ClaimOwnership.claim_id == SupplierClaim.id).where(
        ClaimOwnership.organization_id == org_id, SupplierClaim.id == claim_id)
        .with_for_update())).first()
    if row is None:
        raise HTTPException(404, "Claim not found for this company")
    claim, owner = row
    if claim.status != data.expected_status:
        raise HTTPException(409, "Claim status changed; reload before resolving")
    before = {"status": claim.status, "resolution": claim.resolution}
    claim.status = data.status
    claim.resolution = data.resolution
    after = {"status": claim.status, "resolution": claim.resolution}
    result = claim_out(claim, owner)
    changed_at = datetime.now(timezone.utc)
    edit = ClaimEdit(organization_id=org_id, claim_id=claim_id, edit_key=str(data.edit_key),
                     command_hash=digest, command=command, before_state=before,
                     after_state=after, result=result, actor=actor, created_at=changed_at)
    session.add(edit)
    # Legacy procurement.claim.resolved creates an unowned finance.Payment from a
    # claimed amount. A company-scoped resolution is not evidence of cash receipt.
    core.event_bus.emit(session, "procurement.claim.resolution_recorded", {
        "organization_id": org_id, "claim_id": claim.id, "supplier_id": claim.supplier_id,
        "claim_type": claim.claim_type,
        "amount_byn": None if claim.amount_byn is None else str(claim.amount_byn),
        "resolution": claim.resolution, "status": claim.status,
        "order_id": owner.order_id, "entity_ref": claim.entity_ref,
    })
    return result


@router.get("/organizations/{org_id}/claims/{claim_id}/history")
async def claim_history(org_id: int, claim_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    owner = await session.scalar(select(ClaimOwnership).where(
        ClaimOwnership.organization_id == org_id, ClaimOwnership.claim_id == claim_id))
    if owner is None:
        raise HTTPException(404, "Claim not found for this company")
    edits = (await session.scalars(select(ClaimEdit).where(
        ClaimEdit.organization_id == org_id, ClaimEdit.claim_id == claim_id)
        .order_by(ClaimEdit.id))).all()
    return {"organization_id": org_id, "claim_id": claim_id,
            "created": {"actor": owner.actor, "created_at": owner.created_at,
                        "evidence": owner.evidence, "snapshot": owner.result["claim"]},
            "edits": [{"actor": edit.actor, "created_at": edit.created_at,
                       "before": edit.before_state, "after": edit.after_state}
                      for edit in edits]}
