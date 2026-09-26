"""Company-owned supplier contract references for procurement source documents.

Legacy office contracts have no verified company or supplier identity and are
not silently adopted into this catalogue.
"""

from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.procurement.models import Supplier
from modules.procurement.ownership import plan_context
from modules.procurement.receipt_documents import Input, immutable
from modules.procurement.supplier_identity import active_bound_suppliers

router = APIRouter(tags=["Договоры с поставщиками"])


class SupplierContract(Base):
    __tablename__ = "supplier_contract"
    __table_args__ = (
        CheckConstraint("organization_id > 0", name="supplier_contract_org_positive"),
        CheckConstraint("supplier_id > 0", name="supplier_contract_supplier_positive"),
        CheckConstraint("expires_on IS NULL OR expires_on >= signed_on", name="supplier_contract_valid_dates"),
        Index("uq_supplier_contract_active_identity", "organization_id", "supplier_id", "number",
              unique=True, postgresql_where=text("is_active"), sqlite_where=text("is_active")),
        {"schema": "procurement"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("procurement.supplier.id"), index=True)
    number: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(255))
    signed_on: Mapped[date] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    evidence: Mapped[str] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deactivated_by: Mapped[str | None] = mapped_column(String(200))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(String(500))
    deactivation_key: Mapped[str | None] = mapped_column(String(36))


def guarded_deactivation(mapper, connection, target):
    changed = {attribute.key for attribute in inspect(target).attrs if attribute.history.has_changes()}
    permitted = {"is_active", "deactivated_by", "deactivated_at", "deactivation_reason", "deactivation_key"}
    if (changed != permitted or target.is_active is not False or not target.deactivated_by
            or target.deactivated_at is None or not target.deactivation_reason
            or not target.deactivation_key):
        raise ValueError("Supplier contract can only be deactivated with an audit record")


event.listen(SupplierContract, "before_update", guarded_deactivation)
event.listen(SupplierContract, "before_delete", immutable)


class ContractCreate(Input):
    supplier_id: int = Field(gt=0, strict=True)
    number: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=255)
    signed_on: date
    expires_on: date | None = None
    evidence: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def date_order(self):
        if self.expires_on is not None and self.expires_on < self.signed_on:
            raise ValueError("Contract expiry precedes signature")
        return self


class ContractRevoke(Input):
    key: UUID
    reason: str = Field(min_length=1, max_length=500)


def output(row):
    return {"id": row.id, "organization_id": row.organization_id,
            "supplier_id": row.supplier_id, "number": row.number, "title": row.title,
            "signed_on": row.signed_on, "expires_on": row.expires_on,
            "evidence": row.evidence, "is_active": row.is_active,
            "created_by": row.created_by, "created_at": row.created_at,
            "deactivated_by": row.deactivated_by, "deactivated_at": row.deactivated_at,
            "deactivation_reason": row.deactivation_reason,
            "deactivation_key": row.deactivation_key}


async def read_scope(org_id: int, ctx=Depends(plan_context)):
    session, gateway, user = ctx
    return session, await gateway.source_member(session, org_id, user)


async def write_scope(org_id: int, ctx=Depends(plan_context)):
    session, gateway, user = ctx
    return session, await gateway.source_write_authority(session, org_id, user)


@router.get("/organizations/{org_id}/supplier-contracts")
async def list_contracts(
    org_id: int, supplier_id: int = Query(gt=0),
    status: str = Query(default="active", pattern="^(active|revoked)$"),
    ctx=Depends(read_scope),
):
    session, _ = ctx
    supplier = await session.scalar((active_bound_suppliers() if status == "active" else select(Supplier))
                                    .where(Supplier.id == supplier_id))
    if supplier is None:
        raise HTTPException(409, "Select a directory supplier")
    query = select(SupplierContract).where(
        SupplierContract.organization_id == org_id,
        SupplierContract.supplier_id == supplier_id,
        SupplierContract.is_active.is_(status == "active"),
    )
    query = (query.order_by(SupplierContract.number, SupplierContract.id) if status == "active"
             else query.order_by(SupplierContract.deactivated_at.desc(), SupplierContract.id.desc()))
    rows = (await session.scalars(query.limit(101))).all()
    return {"organization_id": org_id, "supplier_id": supplier_id,
            "status": status, "items": [output(row) for row in rows[:100]], "truncated": len(rows) > 100}


@router.post("/organizations/{org_id}/supplier-contracts", status_code=201)
async def create_contract(org_id: int, data: ContractCreate, ctx=Depends(write_scope)):
    session, actor = ctx
    supplier = await session.scalar(active_bound_suppliers().where(
        Supplier.id == data.supplier_id).with_for_update(read=True))
    if supplier is None:
        raise HTTPException(409, "Select an active directory supplier")
    existing = await session.scalar(select(SupplierContract).where(
        SupplierContract.organization_id == org_id,
        SupplierContract.supplier_id == data.supplier_id,
        SupplierContract.number == data.number,
        SupplierContract.is_active.is_(True),
    ).with_for_update())
    if existing is not None:
        if (not existing.is_active or existing.title != data.title
                or existing.signed_on != data.signed_on
                or existing.expires_on != data.expires_on
                or existing.evidence != data.evidence):
            raise HTTPException(409, "Contract number already identifies different source facts")
        return output(existing)
    row = SupplierContract(organization_id=org_id, supplier_id=data.supplier_id,
                           number=data.number, title=data.title, signed_on=data.signed_on,
                           expires_on=data.expires_on, evidence=data.evidence, created_by=actor)
    session.add(row)
    await session.flush()
    return output(row)


@router.post("/organizations/{org_id}/supplier-contracts/{contract_id}/revoke")
async def revoke_contract(org_id: int, contract_id: int, data: ContractRevoke,
                          ctx=Depends(write_scope)):
    session, actor = ctx
    row = await session.scalar(select(SupplierContract).where(
        SupplierContract.id == contract_id,
        SupplierContract.organization_id == org_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "Supplier contract not found in this organization")
    if not row.is_active:
        if (row.deactivation_key == str(data.key) and row.deactivated_by == actor
                and row.deactivation_reason == data.reason):
            return output(row)
        raise HTTPException(409, "Supplier contract was already revoked with different evidence")
    row.is_active = False
    row.deactivated_by = actor
    row.deactivated_at = datetime.now(timezone.utc)
    row.deactivation_reason = data.reason
    row.deactivation_key = str(data.key)
    await session.flush()
    return output(row)
