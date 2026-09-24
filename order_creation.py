"""Atomic company purchase-order commands and durable no-write reconciliation."""
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator, model_serializer, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import Sku
from modules.procurement.models import PurchaseOrder, PurchaseOrderLine, Supplier
from modules.procurement.ownership import (
    OrderRequestLink,
    PurchaseOwnership,
    effective_plan_user,
    owned_plan_source,
    plan_context,
    plan_writer,
    request_command_hash,
    source_snapshot,
)
from modules.procurement.receipt_documents import Input, immutable


class CanonicalInput(Input):
    @field_validator("*", mode="before")
    @classmethod
    def canonical_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if "\x00" in value:
                raise ValueError("NUL is not supported")
        return value


def fixed_decimal(value, scale, positive=False):
    if not re.fullmatch(rf"(?:0|[1-9][0-9]{{0,{13-scale}}})\.[0-9]{{{scale}}}", value):
        raise ValueError(f"Use an exact Numeric(14,{scale}) decimal string")
    if positive and Decimal(value) <= 0:
        raise ValueError("Quantity must be positive")
    return value


class OrderLine(CanonicalInput):
    sku_code: str = Field(strict=True, min_length=1, max_length=64)
    sku_id: int | None = Field(default=None, strict=True, gt=0, le=2147483647)
    sku_title: str | None = Field(default=None, strict=True, min_length=1, max_length=255)
    sku_unit: str | None = Field(default=None, strict=True, min_length=1, max_length=16)
    qty: str = Field(strict=True)
    goods_value_byn: str = Field(strict=True)
    weight: str = Field(strict=True)
    volume: str = Field(strict=True)

    @field_validator("qty", "goods_value_byn", "weight", "volume")
    @classmethod
    def exact(cls, value, info):
        return fixed_decimal(value, {"qty": 2, "goods_value_byn": 2, "weight": 3, "volume": 4}[info.field_name], info.field_name == "qty")

    @model_validator(mode="after")
    def paired_catalog_snapshot(self):
        values = (self.sku_id, self.sku_title, self.sku_unit)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("SKU ID, title and unit must be supplied together")
        return self

    @model_serializer(mode="wrap")
    def legacy_compatible_serialization(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class OrderDocument(CanonicalInput):
    supplier: str = Field(strict=True, min_length=1, max_length=255)
    supplier_id: int | None = Field(default=None, strict=True, gt=0, le=2147483647)
    supplier_unp: str | None = Field(default=None, strict=True, max_length=32)
    eta_date: str | None
    freight_byn: str = Field(strict=True)
    lines: list[OrderLine] = Field(min_length=1, max_length=200)

    @field_validator("freight_byn")
    @classmethod
    def freight(cls, value):
        return fixed_decimal(value, 2)

    @field_validator("eta_date")
    @classmethod
    def calendar_date(cls, value):
        if value is not None and (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("Use a calendar YYYY-MM-DD date")
        return value

    @model_validator(mode="after")
    def distinct_skus(self):
        if len({x.sku_code for x in self.lines}) != len(self.lines):
            raise ValueError("Duplicate SKU lines are not supported")
        if (self.supplier_id is None) != (self.supplier_unp is None):
            raise ValueError("Supplier ID and UNP snapshot must be supplied together")
        return self

    @model_serializer(mode="wrap")
    def legacy_compatible_serialization(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class RequestBasis(CanonicalInput):
    request_id: int = Field(strict=True, gt=0, le=2147483647)
    expected_stage: Literal["approval"]
    expected_hash: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    link_evidence: str = Field(strict=True, min_length=1, max_length=1000)


class OrderCommand(CanonicalInput):
    request_key: str = Field(strict=True)
    document: OrderDocument
    ownership_evidence: str = Field(strict=True, min_length=1, max_length=1000)
    request_basis: RequestBasis | None

    @field_validator("request_key")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Use a canonical UUID")
        return value


class PurchaseOrderCreation(Base):
    __tablename__ = "purchase_order_creation"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key"), UniqueConstraint("order_id"),
        UniqueConstraint("ownership_id"), UniqueConstraint("request_id"), UniqueConstraint("link_id"),
        CheckConstraint("organization_id > 0", name="order_creation_positive_org"),
        CheckConstraint("outcome IN ('created','rejected')", name="order_creation_outcome"),
        CheckConstraint("(outcome='created' AND order_id IS NOT NULL AND ownership_id IS NOT NULL AND order_id > 0 AND ownership_id > 0) OR (outcome='rejected' AND order_id IS NULL AND ownership_id IS NULL AND request_id IS NULL AND request_ownership_id IS NULL AND link_id IS NULL)", name="order_creation_outcome_refs"),
        CheckConstraint("(request_id IS NULL AND request_ownership_id IS NULL AND link_id IS NULL) OR (request_id IS NOT NULL AND request_ownership_id IS NOT NULL AND link_id IS NOT NULL AND request_id > 0 AND request_ownership_id > 0 AND link_id > 0)", name="order_creation_link_refs"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(16))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_order.id"))
    ownership_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_request.id"))
    request_ownership_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    link_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.order_request_link.id"))
    command: Mapped[dict] = mapped_column(JSON)
    command_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(PurchaseOrderCreation, "before_update", immutable)
event.listen(PurchaseOrderCreation, "before_delete", immutable)
router = APIRouter(tags=["Создание заказов поставщикам"])
REJECTION_CODES = {"request_basis_changed", "request_basis_unavailable", "request_already_linked", "command_abandoned", "sku_catalog_changed", "supplier_catalog_changed"}


def basis_snapshot(owner, row):
    return {"request_id": row.id, "ownership_id": owner.id, "number": row.number,
            "supplier": row.supplier, "supplier_id": row.supplier_id, "item": row.item,
            "qty": str(row.qty), "amount": str(row.amount), "due_date": row.due_date, "stage": row.stage}


async def read_scope(org_id: int, ctx=Depends(plan_context)):
    session, gateway, user = ctx
    await gateway.source_member(session, org_id, user)
    user = await effective_plan_user(session, user)
    return session, await gateway.source_member(session, org_id, user)


@router.get("/organizations/{org_id}/requests/{request_id}/order-basis")
async def order_basis(org_id: int, request_id: int, ctx=Depends(read_scope)):
    session, _ = ctx
    owner, row = await owned_plan_source(session, org_id, "request", request_id)
    if row.stage != "approval" or await session.scalar(select(OrderRequestLink.id).where(OrderRequestLink.request_ownership_id == owner.id)):
        raise HTTPException(409, "An approved unlinked request is required")
    snapshot = basis_snapshot(owner, row)
    return {"organization_id": org_id, "snapshot": snapshot, "basis_hash": request_command_hash(snapshot)}


@router.get("/organizations/{org_id}/orders/{order_id}")
async def scoped_order(org_id: int, order_id: int, after_line_id: int = Query(0, ge=0), ctx=Depends(read_scope)):
    session, _ = ctx
    _, row = await owned_plan_source(session, org_id, "order", order_id)
    lines = (await session.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == row.id,
        PurchaseOrderLine.id > after_line_id).order_by(PurchaseOrderLine.id).limit(201))).all()
    page = lines[:200]
    return {"organization_id": org_id, "id": row.id, "number": row.number, "supplier": row.supplier,
            "status": row.status, "eta_date": row.eta_date.isoformat() if row.eta_date else None,
            "freight_byn": str(row.freight_byn), "lines": [line_result(x) for x in page],
            "next_after_line_id": page[-1].id if len(lines) > 200 else None}


def line_result(line):
    return {"id": line.id, "sku_code": line.sku_code, "qty": str(line.qty),
            "goods_value_byn": str(line.goods_value_byn), "weight": str(line.weight), "volume": str(line.volume)}


async def saved_outcome(session, org, actor, data):
    receipt = await session.scalar(select(PurchaseOrderCreation).where(
        PurchaseOrderCreation.organization_id == org, PurchaseOrderCreation.request_key == data.request_key))
    if receipt is None:
        return None
    if receipt.actor != actor:
        raise HTTPException(403, "Command belongs to another principal")
    if receipt.command != data.model_dump(mode="json"):
        raise HTTPException(409, "Command key belongs to another payload")
    await validate_receipt(session, receipt)
    return receipt.result


async def validate_receipt(session, row):
    def invalid():
        raise HTTPException(409, "Stored order command receipt is inconsistent")
    try:
        command = OrderCommand.model_validate(row.command).model_dump(mode="json")
    except ValueError:
        invalid()
    if command != row.command or request_command_hash(command) != row.command_hash or command["request_key"] != row.request_key:
        invalid()
    result = row.result
    common = {"organization_id": row.organization_id, "request_key": row.request_key, "principal": row.actor, "outcome": row.outcome}
    if row.outcome == "rejected":
        if (not isinstance(result, dict) or result.get("code") not in REJECTION_CODES
                or result != {**common, "code": result.get("code"), "no_business_write": True}
                or any(x is not None for x in (row.order_id, row.ownership_id, row.request_id, row.request_ownership_id, row.link_id))):
            invalid()
        return
    if row.outcome != "created" or not isinstance(result, dict):
        invalid()
    owner = await session.get(PurchaseOwnership, row.ownership_id)
    if owner is None or owner.kind != "order" or owner.source_id != row.order_id or owner.organization_id != row.organization_id or owner.actor != row.actor or owner.evidence != command["ownership_evidence"]:
        invalid()
    if await session.get(PurchaseOrder, row.order_id) is None:
        invalid()
    document = command["document"]
    if owner.snapshot != {"number": result.get("number"), "supplier": document["supplier"], "supplier_id": document.get("supplier_id"), "status": "draft", "eta_date": document["eta_date"]}:
        invalid()
    lines = result.get("lines")
    if not isinstance(lines, list) or len(lines) != len(document["lines"]):
        invalid()
    ids = []
    for line, original in zip(lines, document["lines"], strict=True):
        operational = {key: value for key, value in original.items() if key not in {"sku_id", "sku_title", "sku_unit"}}
        if not isinstance(line, dict) or type(line.get("id")) is not int or line["id"] <= 0 or line != {"id": line["id"], **operational}:
            invalid()
        ids.append(line["id"])
    if len(set(ids)) != len(ids):
        invalid()
    request_snapshot = result.get("request_snapshot")
    basis = command["request_basis"]
    if basis is None:
        if any(x is not None for x in (row.request_id, row.request_ownership_id, row.link_id, request_snapshot)):
            invalid()
    else:
        link = await session.get(OrderRequestLink, row.link_id)
        request_owner = await session.get(PurchaseOwnership, row.request_ownership_id)
        if (link is None or request_owner is None or row.request_id != basis["request_id"]
                or request_owner.kind != "request" or request_owner.source_id != row.request_id
                or request_owner.organization_id != row.organization_id
                or link.organization_id != row.organization_id or link.order_ownership_id != row.ownership_id
                or link.request_ownership_id != row.request_ownership_id or link.actor != row.actor
                or link.evidence != basis["link_evidence"] or not isinstance(request_snapshot, dict)
                or request_snapshot.get("request_id") != row.request_id or request_snapshot.get("ownership_id") != row.request_ownership_id
                or request_snapshot.get("stage") != "approval" or request_command_hash(request_snapshot) != basis["expected_hash"]):
            invalid()
    expected = {**common, "order_id": row.order_id, "ownership_id": row.ownership_id, "number": owner.snapshot["number"],
        "status": "draft", "supplier": document["supplier"], "eta_date": document["eta_date"], "freight_byn": document["freight_byn"],
        "lines": lines, "request_id": row.request_id, "request_ownership_id": row.request_ownership_id,
        "link_id": row.link_id, "request_snapshot": request_snapshot}
    if result != expected:
        invalid()


async def reject_command(session, org, actor, data, code):
    # Durable tombstone: even an original HTTP request arriving after reconciliation
    # cannot execute this key. This writes a command outcome, no business document.
    result = {"organization_id": org, "request_key": data.request_key, "principal": actor,
              "outcome": "rejected", "code": code, "no_business_write": True}
    session.add(PurchaseOrderCreation(organization_id=org, request_key=data.request_key, outcome="rejected",
        command=data.model_dump(mode="json"), command_hash=request_command_hash(data.model_dump(mode="json")), result=result, actor=actor))
    await session.flush()
    return result


def response(result):
    return JSONResponse(status_code=201 if result["outcome"] == "created" else 409, content=result)


@router.post("/organizations/{org_id}/order-commands/reconcile")
async def reconcile_order_command(org_id: int, data: OrderCommand, ctx=Depends(plan_writer)):
    session, actor = ctx
    result = await saved_outcome(session, org_id, actor, data)
    if result is None:
        result = await reject_command(session, org_id, actor, data, "command_abandoned")
    return response(result)


@router.post("/organizations/{org_id}/orders")
async def create_order(org_id: int, data: OrderCommand, ctx=Depends(plan_writer)):
    session, actor = ctx
    existing = await saved_outcome(session, org_id, actor, data)
    if existing is not None:
        return response(existing)
    request_owner = request = snapshot = None
    if data.request_basis:
        basis = data.request_basis
        try:
            request_owner, request = await owned_plan_source(session, org_id, "request", basis.request_id)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return response(await reject_command(session, org_id, actor, data, "request_basis_unavailable"))
        snapshot = basis_snapshot(request_owner, request)
        if request.stage != "approval" or request_command_hash(snapshot) != basis.expected_hash:
            return response(await reject_command(session, org_id, actor, data, "request_basis_changed"))
        if await session.scalar(select(OrderRequestLink.id).where(OrderRequestLink.request_ownership_id == request_owner.id)):
            return response(await reject_command(session, org_id, actor, data, "request_already_linked"))
    doc = data.document
    if doc.supplier_id is not None:
        supplier = await session.scalar(select(Supplier).where(Supplier.id == doc.supplier_id).with_for_update(read=True))
        if (supplier is None or supplier.status != "active" or supplier.name != doc.supplier
                or supplier.unp != doc.supplier_unp):
            return response(await reject_command(session, org_id, actor, data, "supplier_catalog_changed"))
    selected = [line for line in doc.lines if line.sku_id is not None]
    if selected:
        rows = (await session.scalars(select(Sku).where(Sku.id.in_([line.sku_id for line in selected])).with_for_update(read=True))).all()
        skus = {row.id: row for row in rows}
        if any((sku := skus.get(line.sku_id)) is None or not sku.is_active or sku.code != line.sku_code
               or sku.title != line.sku_title or sku.unit != line.sku_unit for line in selected):
            return response(await reject_command(session, org_id, actor, data, "sku_catalog_changed"))
    order = PurchaseOrder(supplier=doc.supplier, supplier_id=doc.supplier_id, status="draft", eta_date=date.fromisoformat(doc.eta_date) if doc.eta_date else None,
                          freight_byn=Decimal(doc.freight_byn))
    session.add(order)
    await session.flush()
    order.number = f"PO-{date.today().year}-{order.id:06d}"
    lines = [PurchaseOrderLine(order_id=order.id, sku_code=x.sku_code, qty=Decimal(x.qty),
        goods_value_byn=Decimal(x.goods_value_byn), weight=Decimal(x.weight), volume=Decimal(x.volume)) for x in doc.lines]
    session.add_all(lines)
    await session.flush()
    owner = PurchaseOwnership(organization_id=org_id, kind="order", source_id=order.id,
        snapshot=await source_snapshot(session, "order", order.id), evidence=data.ownership_evidence, actor=actor)
    session.add(owner)
    await session.flush()
    link = None
    if request is not None:
        link = OrderRequestLink(organization_id=org_id, order_ownership_id=owner.id, request_ownership_id=request_owner.id,
                                evidence=data.request_basis.link_evidence, actor=actor)
        session.add(link)
        request.stage = "po"
        await session.flush()
    result = {"organization_id": org_id, "request_key": data.request_key, "principal": actor, "outcome": "created",
        "order_id": order.id, "ownership_id": owner.id, "number": order.number, "status": "draft",
        "supplier": doc.supplier, "eta_date": doc.eta_date, "freight_byn": doc.freight_byn,
        "lines": [line_result(x) for x in lines], "request_id": request.id if request else None,
        "request_ownership_id": request_owner.id if request_owner else None, "link_id": link.id if link else None,
        "request_snapshot": snapshot}
    receipt = PurchaseOrderCreation(organization_id=org_id, request_key=data.request_key, outcome="created",
        order_id=order.id, ownership_id=owner.id, request_id=result["request_id"], request_ownership_id=result["request_ownership_id"],
        link_id=result["link_id"], command=data.model_dump(mode="json"), command_hash=request_command_hash(data.model_dump(mode="json")),
        result=result, actor=actor)
    session.add(receipt)
    await session.flush()
    await validate_receipt(session, receipt)
    return response(result)
