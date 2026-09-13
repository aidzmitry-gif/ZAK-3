"""Explicit company ownership of legacy procurement sources, never a default company."""
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from config.access import is_package_allowed
from core.db.base import Base
from core.runtime.deps import get_core, get_session
from core.services.auth import (
    EffectiveIdentityLookupError,
    get_current_user,
    resolve_effective_oidc_user,
)
from modules.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseRequest
from modules.procurement.receipt_documents import Input, context, immutable, scoped, transaction


class OwnershipInput(Input):
    kind: Literal["request", "order"]
    source_id: int = Field(gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=1000)
    expected_snapshot: dict | None = None


class OrderRequestInput(Input):
    order_id: int = Field(gt=0, strict=True)
    request_id: int = Field(gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=1000)


class PurchaseOwnership(Base):
    __tablename__ = "purchase_ownership"
    __table_args__ = (UniqueConstraint("kind", "source_id"), CheckConstraint("kind IN ('request','order')", name="purchase_ownership_kind"), CheckConstraint("source_id > 0", name="purchase_ownership_positive_id"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderRequestLink(Base):
    __tablename__ = "order_request_link"
    __table_args__ = (UniqueConstraint("order_ownership_id", "request_ownership_id"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    order_ownership_id: Mapped[int] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    request_ownership_id: Mapped[int] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


for model in (PurchaseOwnership, OrderRequestLink):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)

router = APIRouter(tags=["Юрлица и основания закупок"])


def serialize(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def current_read_scope(org_id: int, ctx=Depends(context)):
    session, gateway, user = ctx
    user = await effective_plan_user(session, user)
    await gateway.source_member(session, org_id, user)
    user = await effective_plan_user(session, user)
    return session, await gateway.source_member(session, org_id, user)


@router.get("/organizations/{org_id}/owned-sources")
async def owned_sources(org_id: int, kind: Literal["order", "request"],
                        after_id: int = Query(0, ge=0), q: str = Query("", max_length=200),
                        ctx=Depends(current_read_scope)):
    session, _ = ctx
    model = PurchaseOrder if kind == "order" else PurchaseRequest
    query = select(model).join(PurchaseOwnership, (PurchaseOwnership.source_id == model.id) &
                              (PurchaseOwnership.kind == kind)).where(
        PurchaseOwnership.organization_id == org_id, model.id > after_id)
    if q.strip():
        terms = [model.number.icontains(q.strip(), autoescape=True), model.supplier.icontains(q.strip(), autoescape=True)]
        if kind == "request":
            terms.append(model.item.icontains(q.strip(), autoescape=True))
        query = query.where(or_(*terms))
    rows = (await session.scalars(query.order_by(model.id).limit(51))).all()
    page = rows[:50]
    lines = {}
    if kind == "order" and page:
        for line in (await session.scalars(select(PurchaseOrderLine).where(
                PurchaseOrderLine.order_id.in_([row.id for row in page])).order_by(PurchaseOrderLine.id))).all():
            lines.setdefault(line.order_id, []).append({"id": line.id, "sku": line.sku_code,
                "quantity": str(line.qty), "goods_value_byn": str(line.goods_value_byn)})
    items = []
    for row in page:
        item = {"id": row.id, "number": row.number, "supplier": row.supplier}
        if kind == "order":
            item.update(status=row.status, eta_date=row.eta_date,
                        freight_byn=str(row.freight_byn), lines=lines.get(row.id, []))
        else:
            item.update(stage=row.stage, item=row.item, quantity=str(row.qty),
                        planned_amount=str(row.amount), due_date=row.due_date)
        items.append(item)
    return {"organization_id": org_id, "items": items,
            "next_after_id": page[-1].id if len(rows) > 50 else None}


async def require_source_access(kind, source_id, ctx):
    session, gateway, user = ctx
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.kind == kind, PurchaseOwnership.source_id == source_id))
    if owner is None:
        model = PurchaseOrder if kind == "order" else PurchaseRequest
        if await session.get(model, source_id) is None:
            raise HTTPException(404, "Procurement source not found")
        raise HTTPException(409, "Confirm the document organization before editing")
    await gateway.source_member(session, owner.organization_id, user)
    model = PurchaseOrder if kind == "order" else PurchaseRequest
    row = await session.scalar(select(model).where(model.id == source_id).with_for_update())
    if row is None:
        raise HTTPException(404, "Procurement source not found")
    return owner.organization_id


async def legacy_access_context(session=Depends(get_session), core=Depends(get_core), user=Depends(get_current_user)):
    # Existing handlers own their commit. Authentication must not add another one.
    return await context(session, core, user)


async def require_order_access(
    order_id: int,
    x_expected_organization: int | None = Header(default=None, gt=0, le=2147483647),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
    ctx=Depends(legacy_access_context),
):
    session, gateway, user = ctx
    user = await effective_plan_user(session, user)
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.kind == "order", PurchaseOwnership.source_id == order_id))
    if owner is None:
        if await session.get(PurchaseOrder, order_id) is None:
            raise HTTPException(404, "Procurement source not found")
        raise HTTPException(409, "Confirm the document organization before editing")
    await gateway.source_owner_authority(session, owner.organization_id, user)
    user = await effective_plan_user(session, user)
    actor = await gateway.source_owner_authority(session, owner.organization_id, user)
    if x_expected_organization is None or x_expected_principal is None:
        raise HTTPException(422, "Expected organization and principal headers are required")
    if owner.organization_id != x_expected_organization or actor != x_expected_principal:
        raise HTTPException(409, "Order context differs from the expected organization or principal")
    row = await session.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order_id)
        .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "Procurement source not found")
    return owner.organization_id


async def require_request_access(req_id: int, ctx=Depends(legacy_access_context)):
    return await require_source_access("request", req_id, ctx)


@router.get("/organizations/{org_id}/purchase-ownership/candidates")
async def ownership_candidates(org_id: int, kind: Literal["order", "request"],
                               q: str = Query("", max_length=200), after_id: int = Query(0, ge=0),
                               ctx=Depends(context)):
    session, gateway, user = ctx
    await gateway.source_owner_authority(session, org_id, user)
    model = PurchaseOrder if kind == "order" else PurchaseRequest
    assigned = select(PurchaseOwnership.id).where(
        PurchaseOwnership.kind == kind, PurchaseOwnership.source_id == model.id,
    ).exists()
    statement = select(model).where(model.id > after_id, ~assigned)
    if q.strip():
        terms = [model.number.icontains(q.strip(), autoescape=True), model.supplier.icontains(q.strip(), autoescape=True)]
        if kind == "request":
            terms.append(model.item.icontains(q.strip(), autoescape=True))
        statement = statement.where(or_(*terms))
    rows = (await session.scalars(statement.order_by(model.id).limit(51))).all()
    page = rows[:50]
    return {"items": [{"source_id": row.id, "number": row.number, "supplier": row.supplier,
                       "item": row.item if kind == "request" else None} for row in page],
            "next_after_id": page[-1].id if len(rows) > 50 else None}


async def source_snapshot(session, kind, source_id):
    model = PurchaseOrder if kind == "order" else PurchaseRequest
    source = await session.scalar(select(model).where(model.id == source_id).with_for_update())
    if source is None:
        raise HTTPException(404, "Procurement source not found")
    snapshot = {"number": source.number, "supplier": source.supplier, "supplier_id": source.supplier_id}
    if kind == "request":
        snapshot.update(item=source.item, quantity=str(source.qty), planned_amount=str(source.amount), due_date=source.due_date)
    else:
        snapshot.update(status=source.status, eta_date=str(source.eta_date) if source.eta_date else None)
    return snapshot


@router.post("/organizations/{org_id}/purchase-ownership/preview")
async def preview_ownership(org_id: int, data: OwnershipInput, ctx=Depends(context)):
    session, gateway, user = ctx
    await gateway.source_owner_authority(session, org_id, user)
    existing = await session.scalar(select(PurchaseOwnership).where(PurchaseOwnership.kind == data.kind, PurchaseOwnership.source_id == data.source_id))
    if existing:
        if existing.organization_id != org_id:
            raise HTTPException(409, "Source already has an ownership decision")
        return {"snapshot": existing.snapshot}
    snapshot = await source_snapshot(session, data.kind, data.source_id)
    # Another company may have committed ownership while this transaction
    # waited for the legacy source lock. Recheck under that shared lock.
    existing = await session.scalar(select(PurchaseOwnership).where(PurchaseOwnership.kind == data.kind, PurchaseOwnership.source_id == data.source_id))
    if existing:
        if existing.organization_id != org_id:
            raise HTTPException(409, "Source already has an ownership decision")
        snapshot = existing.snapshot
    return {"snapshot": snapshot}


@router.get("/organizations/{org_id}/purchase-ownership")
async def ownership_list(org_id: int, ctx=Depends(current_read_scope)):
    rows = (await ctx[0].scalars(select(PurchaseOwnership).where(PurchaseOwnership.organization_id == org_id).order_by(PurchaseOwnership.id))).all()
    return [serialize(row) for row in rows]


@router.post("/organizations/{org_id}/purchase-ownership", status_code=201)
async def assign_ownership(org_id: int, data: OwnershipInput, ctx=Depends(context)):
    session, gateway, user = ctx
    actor = await gateway.source_owner_authority(session, org_id, user)
    existing = await session.scalar(select(PurchaseOwnership).where(PurchaseOwnership.kind == data.kind, PurchaseOwnership.source_id == data.source_id))
    if existing:
        if existing.organization_id != org_id or existing.evidence != data.evidence or (data.expected_snapshot is not None and data.expected_snapshot != existing.snapshot):
            raise HTTPException(409, "Source already has an ownership decision; a reviewed correction is required")
        return serialize(existing)
    snapshot = await source_snapshot(session, data.kind, data.source_id)
    if data.expected_snapshot is not None and snapshot != data.expected_snapshot:
        raise HTTPException(409, "Source changed after preview; review it again")
    row = PurchaseOwnership(organization_id=org_id, kind=data.kind, source_id=data.source_id,
                            snapshot=snapshot, evidence=data.evidence, actor=actor)
    session.add(row)
    await session.flush()
    return serialize(row)


@router.get("/organizations/{org_id}/order-request-links")
async def link_list(org_id: int, ctx=Depends(current_read_scope)):
    rows = (await ctx[0].scalars(select(OrderRequestLink).where(OrderRequestLink.organization_id == org_id).order_by(OrderRequestLink.id))).all()
    return [serialize(row) for row in rows]


@router.post("/organizations/{org_id}/order-request-links", status_code=201)
async def link_order_request(org_id: int, data: OrderRequestInput, ctx=Depends(scoped)):
    session, actor = ctx
    ids = []
    for kind, source_id in [("order", data.order_id), ("request", data.request_id)]:
        row = await session.scalar(select(PurchaseOwnership).where(PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == kind, PurchaseOwnership.source_id == source_id))
        if row is None:
            raise HTTPException(409, "Both sources must have confirmed ownership in this organization")
        ids.append(row.id)
    existing = await session.scalar(select(OrderRequestLink).where(OrderRequestLink.order_ownership_id == ids[0], OrderRequestLink.request_ownership_id == ids[1]))
    if existing:
        if existing.evidence != data.evidence:
            raise HTTPException(409, "Link evidence cannot be replaced")
        return serialize(existing)
    row = OrderRequestLink(organization_id=org_id, order_ownership_id=ids[0], request_ownership_id=ids[1], evidence=data.evidence, actor=actor)
    session.add(row)
    await session.flush()
    return serialize(row)

# The request-creation command is independent of the physical receipt workflow.
class RequestDocument(Input):
    supplier: str = Field(min_length=1, max_length=255, strict=True)
    item: str = Field(min_length=1, max_length=255, strict=True)
    qty: int = Field(gt=0, le=2147483647, strict=True)
    amount: str = Field(strict=True)
    due_date: str | None = None

    @field_validator("supplier", "item", mode="before")
    @classmethod
    def canonical_text(cls, value):
        # Match the receipt guard, including Python's U+001C..U+001F whitespace.
        return value.strip() if isinstance(value, str) else value

    @field_validator("amount")
    @classmethod
    def exact_amount(cls, value):
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})\.[0-9]{2}", value):
            raise ValueError("Use a nonnegative exact decimal string with two places (Numeric(14,2))")
        return value

    @field_validator("due_date")
    @classmethod
    def iso_date(cls, value):
        if value is not None and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("Use an ISO date")
        return value


class RequestCreate(Input):
    request_key: str = Field(strict=True)
    document: RequestDocument
    ownership_evidence: str = Field(min_length=1, max_length=1000, strict=True)

    @field_validator("ownership_evidence", mode="before")
    @classmethod
    def canonical_evidence(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("request_key")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Use a canonical UUID")
        return value


PlanningStage = Literal["need", "sourcing", "nego", "analysis", "approval"]
PLANNING_STAGES = ("need", "sourcing", "nego", "analysis", "approval")


class RequestStage(Input):
    expected_stage: PlanningStage
    stage: PlanningStage


class RequestOrderLink(Input):
    expected_stage: Literal["approval"]
    order_id: int = Field(gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=1000, strict=True)


class PurchaseRequestCreation(Base):
    __tablename__ = "purchase_request_creation"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key"),
        UniqueConstraint("request_id"), UniqueConstraint("ownership_id"),
        CheckConstraint("organization_id > 0 AND request_id > 0 AND ownership_id > 0", name="request_creation_positive_refs"),
        CheckConstraint("length(request_key) = 36 AND length(command_hash) = 64", name="request_creation_key_hash_length"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(36))
    request_id: Mapped[int] = mapped_column(ForeignKey("procurement.purchase_request.id"))
    ownership_id: Mapped[int] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    command: Mapped[dict] = mapped_column(JSON)
    command_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(PurchaseRequestCreation, "before_update", immutable)
event.listen(PurchaseRequestCreation, "before_delete", immutable)


def request_command_hash(command):
    return hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


async def effective_plan_user(session, user):
    try:
        user = await resolve_effective_oidc_user(user, session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Current identity cannot be verified") from exc
    if user.local_status not in (None, "active"):
        raise HTTPException(403, "An active user is required")
    if not is_package_allowed("procurement", user.roles):
        raise HTTPException(403, "Procurement access required")
    return user


async def plan_context(session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(get_current_user)):
    user = await effective_plan_user(session, user)
    return await context(session, core, user)


@router.get("/organizations/{org_id}/request-plan-context")
async def request_plan_context(org_id: int, ctx=Depends(plan_context)):
    session, gateway, user = ctx
    actor = await gateway.source_member(session, org_id, user)
    can_manage = True
    try:
        await gateway.source_owner_authority(session, org_id, user)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        can_manage = False
    return {"organization_id": org_id, "principal": actor, "can_manage": can_manage}


async def plan_writer(org_id: int, x_expected_principal: str = Header(min_length=1), ctx=Depends(plan_context)):
    session, gateway, user = ctx
    actor = await gateway.source_owner_authority(session, org_id, user)
    # The organization lock may have waited while local employee access changed.
    user = await effective_plan_user(session, user)
    actor = await gateway.source_owner_authority(session, org_id, user)
    # A browser session may change after the recovery journal was opened.
    # This is a comparison guard, never an authentication/actor override.
    if actor != x_expected_principal:
        raise HTTPException(409, "Current principal differs from the saved command")
    return session, actor


async def validate_creation(session, receipt):
    try:
        parsed = RequestCreate.model_validate(receipt.command).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(409, "Stored creation command is invalid") from exc
    owner = await session.get(PurchaseOwnership, receipt.ownership_id, populate_existing=True)
    request = await session.get(PurchaseRequest, receipt.request_id, populate_existing=True)
    result = receipt.result
    if (not isinstance(result, dict) or parsed != receipt.command or parsed["request_key"] != receipt.request_key
            or request_command_hash(parsed) != receipt.command_hash
            or owner is None or request is None
            or owner.organization_id != receipt.organization_id or owner.kind != "request"
            or owner.source_id != request.id or owner.actor != receipt.actor
            or owner.evidence != parsed["ownership_evidence"]
            or owner.snapshot != {"number": result.get("number"), "supplier": parsed["document"]["supplier"],
                                  "supplier_id": None, "item": parsed["document"]["item"],
                                  "quantity": str(parsed["document"]["qty"]),
                                  "planned_amount": parsed["document"]["amount"], "due_date": parsed["document"]["due_date"]}
            or result != {"organization_id": receipt.organization_id, "request_key": receipt.request_key,
                          "request_id": receipt.request_id, "ownership_id": receipt.ownership_id,
                          "number": owner.snapshot.get("number"), "stage": "need"}):
        raise HTTPException(409, "Creation receipt references or hash are inconsistent")
    return result


@router.post("/organizations/{org_id}/requests", status_code=201)
async def create_owned_request(org_id: int, data: RequestCreate, ctx=Depends(plan_writer)):
    session, actor = ctx  # organization lock is already held, including absent keys
    command = data.model_dump(mode="json")
    receipt = await session.scalar(select(PurchaseRequestCreation).where(
        PurchaseRequestCreation.organization_id == org_id, PurchaseRequestCreation.request_key == data.request_key))
    if receipt is not None:
        result = await validate_creation(session, receipt)
        if receipt.command != command:
            raise HTTPException(409, "Request key already belongs to another command")
        return result  # Historical creation result, not the current planning stage.
    row = PurchaseRequest(**{**command["document"], "amount": Decimal(data.document.amount)}, stage="need", origin="")
    session.add(row)
    await session.flush()
    row.number = f"ЗАК-{date.today().year}-{row.id:06d}"
    await session.flush()
    owner = PurchaseOwnership(organization_id=org_id, kind="request", source_id=row.id,
        snapshot=await source_snapshot(session, "request", row.id), evidence=data.ownership_evidence, actor=actor)
    session.add(owner)
    await session.flush()
    result = {"organization_id": org_id, "request_key": data.request_key, "request_id": row.id,
              "ownership_id": owner.id, "number": row.number, "stage": "need"}
    receipt = PurchaseRequestCreation(organization_id=org_id, request_key=data.request_key,
        request_id=row.id, ownership_id=owner.id, command=command, command_hash=request_command_hash(command),
        result=result, actor=actor)
    session.add(receipt)
    await session.flush()
    await validate_creation(session, receipt)
    return result


async def owned_plan_source(session, org_id, kind, source_id):
    owner = await session.scalar(select(PurchaseOwnership).where(
        PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == kind,
        PurchaseOwnership.source_id == source_id))
    if owner is None:
        raise HTTPException(409, "Source must belong to the selected organization")
    model = PurchaseOrder if kind == "order" else PurchaseRequest
    row = await session.scalar(select(model).where(model.id == source_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(409, "Owned source is missing")
    return owner, row


@router.patch("/organizations/{org_id}/requests/{request_id}/stage")
async def move_request_stage(org_id: int, request_id: int, data: RequestStage, ctx=Depends(plan_writer)):
    session, _ = ctx
    if PLANNING_STAGES.index(data.stage) != PLANNING_STAGES.index(data.expected_stage) + 1:
        raise HTTPException(409, "Only the immediate next planning stage is allowed")
    _, row = await owned_plan_source(session, org_id, "request", request_id)
    if row.stage not in (data.expected_stage, data.stage):
        raise HTTPException(409, "Planning stage changed; reload the request")
    row.stage = data.stage
    await session.flush()
    return {"organization_id": org_id, "request_id": row.id, "stage": row.stage}


@router.post("/organizations/{org_id}/requests/{request_id}/order-link", status_code=201)
async def plan_order_link(org_id: int, request_id: int, data: RequestOrderLink, ctx=Depends(plan_writer)):
    session, actor = ctx
    # Consistent organization -> order -> request source lock order.
    order_owner, order = await owned_plan_source(session, org_id, "order", data.order_id)
    request_owner, request = await owned_plan_source(session, org_id, "request", request_id)
    links = (await session.scalars(select(OrderRequestLink).where(
        OrderRequestLink.request_ownership_id == request_owner.id))).all()
    if links:
        if (len(links) == 1 and links[0].organization_id == org_id
                and links[0].order_ownership_id == order_owner.id and links[0].evidence == data.evidence
                and request.stage == "po"):
            return serialize(links[0])
        raise HTTPException(409, "Request link already exists; a reviewed correction is required")
    if request.stage != data.expected_stage or order.status == "cancelled":
        raise HTTPException(409, "An approved request and a non-cancelled order are required")
    link = OrderRequestLink(organization_id=org_id, order_ownership_id=order_owner.id,
        request_ownership_id=request_owner.id, evidence=data.evidence, actor=actor)
    session.add(link)
    request.stage = "po"
    await session.flush()
    return serialize(link)
