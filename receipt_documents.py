"""Procurement-owned receipt drafts and immutable source revisions.

No accounting or warehouse movements are created by saving a draft.
"""
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from config.access import is_package_allowed
from core.db.base import Base
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user
from core.services.procurement import ReceiptAccountingConfirmation, ReceiptAccountingOptions


def exact(value):
    if isinstance(value, (float, bool)) or value is None:
        raise ValueError("Use exact decimal strings")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Amount must be finite")
    return result


Money = Annotated[Decimal, BeforeValidator(exact), Field(ge=0, max_digits=20, decimal_places=2)]
Quantity = Annotated[Decimal, BeforeValidator(exact), Field(gt=0, max_digits=24, decimal_places=6)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReceiptItem(Input):
    order_id: int | None = Field(default=None, gt=0, strict=True)
    order_line_id: int | None = Field(default=None, gt=0, strict=True, exclude_if=lambda value: value is None)
    sku: str = Field(min_length=1, max_length=200)
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    lot: str = Field(min_length=1, max_length=200)
    quantity: Quantity
    net_amount: Money
    vat_rate: Money = Field(le=100)
    vat_amount: Money
    vat_basis: str = Field(min_length=1, max_length=200)


class ReceiptContent(Input):
    currency: Literal["BYN"]
    invoice_reference: str = Field(min_length=1, max_length=200)
    document_date: date
    operation_date: date
    supplier: str = Field(min_length=1, max_length=255)
    supplier_id: int | None = Field(default=None, gt=0, strict=True)
    supplier_unp: str | None = Field(default=None, max_length=32, strict=True)
    contract: str = Field(min_length=1, max_length=200)
    warehouse: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=700)
    items: list[ReceiptItem] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def selected_supplier_pair(self):
        if (self.supplier_id is None) != (self.supplier_unp is None):
            raise ValueError("Supplier ID and UNP snapshot must be selected together")
        return self


class ReceiptCreate(Input):
    key: str = Field(min_length=1, max_length=160)
    document: ReceiptContent


class ReceiptEdit(Input):
    expected_version: int = Field(ge=1, strict=True)
    document: ReceiptContent


class ReceiptAccounts(ReceiptAccountingOptions):
    """Compatibility name for the shared primary-source preparation contract."""


class ReceiptConfirm(ReceiptAccountingConfirmation):
    """Compatibility name for the shared confirmation contract."""


@dataclass(frozen=True)
class PreparedReceipt:
    document: dict
    verified_counterparty_id: int | None


class ReceiptDocument(Base):
    __tablename__ = "receipt_document"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    source_key: Mapped[str] = mapped_column(String(160))
    current_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReceiptRevision(Base):
    __tablename__ = "receipt_revision"
    __table_args__ = (UniqueConstraint("receipt_id", "version"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("procurement.receipt_document.id"))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(mapper, connection, target):
    raise ValueError("Source revisions are immutable; append a new draft version")


class ReceiptPosting(Base):
    __tablename__ = "receipt_posting"
    __table_args__ = {"schema": "procurement"}
    receipt_id: Mapped[int] = mapped_column(ForeignKey("procurement.receipt_document.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    entry_id: Mapped[int] = mapped_column(Integer, unique=True)
    options: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(ReceiptPosting, "before_update", immutable)
event.listen(ReceiptPosting, "before_delete", immutable)


event.listen(ReceiptRevision, "before_update", immutable)
event.listen(ReceiptRevision, "before_delete", immutable)
router = APIRouter(tags=["Первичные накладные закупок"])


async def transaction(session=Depends(get_session)):
    try:
        yield session
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Concurrent receipt change or duplicate source key") from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        await session.rollback()
        raise


async def context(session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(get_current_user)):
    if not is_package_allowed("procurement", user.roles):
        raise HTTPException(403, "Procurement access required")
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization access service is unavailable")
    return session, gateway, user


async def scoped(org_id: int, ctx=Depends(context)):
    session, gateway, user = ctx
    return session, await gateway.source_member(session, org_id, user)


async def output(session, row):
    posted = await session.get(ReceiptPosting, row.id)
    if posted and posted.version > row.current_version:
        posted = None
    revisions = (await session.scalars(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == row.id, ReceiptRevision.version <= row.current_version,
    ).order_by(ReceiptRevision.version))).all()
    return {"id": row.id, "organization_id": row.organization_id, "key": row.source_key,
            "status": "posted" if posted else row.status, "version": row.current_version,
            "posting": {"entry_id": posted.entry_id, "version": posted.version} if posted else None,
            "revisions": [{"version": r.version, "document": r.document,
                           "actor": r.actor, "created_at": r.created_at} for r in revisions]}


@router.get("/receipt-organizations")
async def organizations(ctx=Depends(context)):
    return await ctx[1].source_organizations(ctx[0], ctx[2])


@router.get("/organizations/{org_id}/receipt-postings/{entry_id}")
async def posted_source(org_id: int, entry_id: int, ctx=Depends(scoped)):
    session, _ = ctx
    linked = (await session.execute(select(ReceiptPosting, ReceiptDocument).join(
        ReceiptDocument, ReceiptDocument.id == ReceiptPosting.receipt_id,
    ).where(ReceiptDocument.organization_id == org_id, ReceiptPosting.entry_id == entry_id))).first()
    if linked is None:
        raise HTTPException(404, "Posted receipt source not found in this organization")
    posting, receipt = linked
    revision = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == receipt.id, ReceiptRevision.version == posting.version,
    ))
    if revision is None:
        raise HTTPException(409, "Posted receipt revision is missing")
    return {"organization_id": org_id, "receipt_id": receipt.id, "entry_id": posting.entry_id,
            "version": posting.version, "document": revision.document}


@router.get("/organizations/{org_id}/receipt-documents")
async def list_documents(org_id: int, ctx=Depends(scoped)):
    rows = (await ctx[0].scalars(select(ReceiptDocument).where(
        ReceiptDocument.organization_id == org_id,
    ).order_by(ReceiptDocument.id.desc()))).all()
    return [await output(ctx[0], row) for row in rows]


@router.post("/organizations/{org_id}/receipt-documents", status_code=201)
async def create_document(org_id: int, data: ReceiptCreate, ctx=Depends(scoped), access=Depends(context)):
    session, actor = ctx
    existing = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.organization_id == org_id, ReceiptDocument.source_key == data.key,
    ).with_for_update())
    payload = data.document.model_dump(mode="json")
    if existing:
        first = await session.scalar(select(ReceiptRevision).where(
            ReceiptRevision.receipt_id == existing.id, ReceiptRevision.version == 1,
        ))
        if ReceiptContent.model_validate(first.document).model_dump(mode="json") != payload:
            raise HTTPException(409, "Source key already identifies a different receipt")
        return await output(session, existing)
    await validate_supplier(session, data.document)
    await validate_orders(session, org_id, data.document)
    row = ReceiptDocument(organization_id=org_id, source_key=data.key, current_version=1,
                          status="draft", created_by=actor)
    session.add(row)
    await session.flush()
    session.add(ReceiptRevision(receipt_id=row.id, version=1, document=payload, actor=actor))
    await access[1].source_changed(session, org_id, access[2], f"procurement:receipt:{row.id}", 1, payload["operation_date"])
    await session.flush()
    return await output(session, row)


@router.put("/organizations/{org_id}/receipt-documents/{receipt_id}")
async def edit_document(org_id: int, receipt_id: int, data: ReceiptEdit, ctx=Depends(scoped), access=Depends(context)):
    session, actor = ctx
    row = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == org_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "Receipt not found")
    if row.status != "draft" or row.current_version != data.expected_version or await session.get(ReceiptPosting, row.id):
        raise HTTPException(409, "Receipt is not an editable draft at the expected version")
    await validate_supplier(session, data.document)
    await validate_orders(session, org_id, data.document)
    row.current_version += 1
    session.add(ReceiptRevision(receipt_id=row.id, version=row.current_version,
                                document=data.document.model_dump(mode="json"), actor=actor))
    await access[1].source_changed(session, org_id, access[2], f"procurement:receipt:{row.id}", row.current_version, data.document.operation_date.isoformat())
    await session.flush()
    return await output(session, row)


async def validate_supplier(session, document):
    if document.supplier_id is None:
        raise HTTPException(422, "Select supplier from procurement catalogue")
    from modules.procurement.supplier_identity import selected_supplier

    supplier = await selected_supplier(session, document.supplier_id, document.supplier, document.supplier_unp)
    if supplier is None:
        raise HTTPException(409, "Supplier or MDM identity changed; select the active supplier again")
    return supplier


async def validate_orders(session, org_id, document):
    # Local import avoids the router/context import cycle within this module.
    from modules.procurement.models import PurchaseOrderLine
    from modules.procurement.ownership import PurchaseOwnership

    for item in document.items:
        if item.order_line_id is not None and item.order_id is None:
            raise HTTPException(422, "An order line requires its purchase order")

    ids = {item.order_id for item in document.items if item.order_id is not None}
    if not ids:
        return
    owned = set((await session.scalars(select(PurchaseOwnership.source_id).where(
        PurchaseOwnership.organization_id == org_id, PurchaseOwnership.kind == "order",
        PurchaseOwnership.source_id.in_(ids),
    ))).all())
    if owned != ids:
        raise HTTPException(409, "Every linked order must have confirmed ownership in this organization")
    for item in document.items:
        if item.order_line_id is not None:
            line = await session.scalar(select(PurchaseOrderLine).where(
                PurchaseOrderLine.id == item.order_line_id, PurchaseOrderLine.order_id == item.order_id)
                .with_for_update().execution_options(populate_existing=True))
            if line is None or line.sku_code != item.sku:
                raise HTTPException(409, "Receipt order line must match the exact order and SKU")


def saved_posting_options(posting):
    if not isinstance(posting.options, dict):
        raise ValueError("Stored receipt posting options are invalid")
    options = dict(posting.options)
    marker = object()
    identity = options.pop("supplier_counterparty_id", marker)
    if identity is not marker and (type(identity) is not int or identity <= 0):
        raise ValueError("Stored receipt counterparty identity is invalid")
    return options, None if identity is marker else identity


async def prepare_receipt(session, org_id, receipt_id, data, *, require_current_supplier=False,
                          frozen_counterparty_id=None):
    row = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == org_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "Receipt not found")
    if row.current_version != data.expected_version:
        raise HTTPException(409, "Receipt version changed; reopen the document")
    revision = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == row.id, ReceiptRevision.version == row.current_version,
    ))
    if revision is None:
        raise HTTPException(409, "Receipt source revision is missing")
    facts = revision.document
    if row.status == "draft" and facts.get("supplier_id") is None:
        raise HTTPException(409, "Match the draft supplier to the catalogue before posting")
    verified_counterparty_id = frozen_counterparty_id
    if require_current_supplier:
        supplier = await validate_supplier(session, ReceiptContent.model_validate(facts))
        verified_counterparty_id = supplier.counterparty_id
    if len(data.inventory_accounts) != len(facts["items"]):
        raise HTTPException(422, "Select an inventory account for every source line")
    document = {**{k: v for k, v in facts.items() if k not in {"currency", "supplier", "supplier_id", "supplier_unp", "items"}},
            "source": f"procurement:receipt:{row.id}", "source_version": row.current_version,
            "counterparty": facts["supplier"], "posting_date": data.posting_date.isoformat(),
            "policy_id": data.policy_id, "settlement_account": data.settlement_account,
            "vat_account": data.vat_account,
            "items": [{**{k: v for k, v in item.items() if k != "order_line_id"}, "account": account} for item, account in zip(facts["items"], data.inventory_accounts, strict=True)]}
    return PreparedReceipt(document, verified_counterparty_id)


@router.post("/organizations/{org_id}/receipt-documents/{receipt_id}/preview")
async def preview_document(org_id: int, receipt_id: int, data: ReceiptAccounts, ctx=Depends(context)):
    session, gateway, user = ctx
    await gateway.source_member(session, org_id, user)
    prepared = await prepare_receipt(session, org_id, receipt_id, data, require_current_supplier=True)
    return await gateway.receipt_posting(session, org_id, user, prepared.document, confirm_digest=None,
                                         verified_counterparty_id=prepared.verified_counterparty_id)


@router.post("/organizations/{org_id}/receipt-documents/{receipt_id}/confirm", status_code=201)
async def confirm_document(org_id: int, receipt_id: int, data: ReceiptConfirm, ctx=Depends(context), core=Depends(get_core)):
    session, gateway, user = ctx
    return await confirm_receipt(session, org_id, receipt_id, data, user, gateway, core.services.event_bus)


async def confirm_receipt(session, org_id, receipt_id, data, user, gateway, event_bus):
    """Canonical atomic command shared by procurement and accounting entry points."""
    actor = await gateway.source_member(session, org_id, user)
    previous = await session.get(ReceiptPosting, receipt_id)
    options = data.model_dump(mode="json", exclude={"digest"})
    frozen_identity = None
    if previous:
        try:
            saved_options, frozen_identity = saved_posting_options(previous)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if saved_options != options or previous.digest != data.digest:
            raise HTTPException(409, "Receipt already posted with different accounting settings")
    prepared = await prepare_receipt(session, org_id, receipt_id, data,
                                     require_current_supplier=previous is None,
                                     frozen_counterparty_id=frozen_identity)
    result = await gateway.receipt_posting(session, org_id, user, prepared.document,
                                           confirm_digest=data.digest, event_bus=event_bus,
                                           verified_counterparty_id=prepared.verified_counterparty_id)
    if not previous:
        options["supplier_counterparty_id"] = prepared.verified_counterparty_id
        session.add(ReceiptPosting(receipt_id=receipt_id, version=data.expected_version,
                                   entry_id=result["entry_id"], options=options,
                                   digest=data.digest, actor=actor))
        await session.flush()
    return result
