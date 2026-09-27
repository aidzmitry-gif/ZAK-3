"""Procurement-owned receipt drafts and immutable source revisions.

No accounting or warehouse movements are created by saving a draft.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
from core.domain.models import Sku
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user
from core.services.procurement import (
    ReceiptAccountingConfirmation,
    ReceiptAccountingOptions,
)


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
    sku_id: int | None = Field(default=None, gt=0, strict=True)
    sku_title: str | None = Field(default=None, min_length=1, max_length=255, strict=True)
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
    contract_id: int | None = Field(default=None, gt=0, strict=True,
                                    exclude_if=lambda value: value is None)
    contract_basis: str | None = Field(default=None, min_length=1, max_length=500,
                                       exclude_if=lambda value: value is None)
    warehouse: str = Field(min_length=1, max_length=200)
    warehouse_id: int | None = Field(default=None, gt=0, strict=True,
                                     exclude_if=lambda value: value is None)
    warehouse_title: str | None = Field(default=None, min_length=1, max_length=255, strict=True,
                                        exclude_if=lambda value: value is None)
    explanation: str = Field(min_length=1, max_length=700)
    items: list[ReceiptItem] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def selected_supplier_pair(self):
        if (self.supplier_id is None) != (self.supplier_unp is None):
            raise ValueError("Supplier ID and UNP snapshot must be selected together")
        if (self.warehouse_id is None) != (self.warehouse_title is None):
            raise ValueError("Warehouse ID and title snapshot must be selected together")
        if self.contract_id is not None and self.contract_basis is not None:
            raise ValueError("Select a registered contract or explain the unregistered basis")
        return self


class ReceiptCreate(Input):
    key: str = Field(min_length=1, max_length=160)
    document: ReceiptContent


class ReceiptEdit(Input):
    key: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1, strict=True)
    document: ReceiptContent


class ReceiptCorrectionDraft(Input):
    """Idempotent link to the exact posted receipt being corrected."""

    key: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1, strict=True)
    expected_entry_id: int = Field(gt=0, strict=True)


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
    __table_args__ = (
        UniqueConstraint("organization_id", "source_key"),
        CheckConstraint(
            "(correction_of_receipt_id IS NULL AND correction_of_version IS NULL AND correction_of_entry_id IS NULL) "
            "OR (correction_of_receipt_id IS NOT NULL AND correction_of_version IS NOT NULL AND correction_of_entry_id IS NOT NULL)",
            name="receipt_document_correction_complete",
        ),
        CheckConstraint("correction_of_receipt_id IS NULL OR correction_of_receipt_id <> id",
                        name="receipt_document_correction_not_self"),
        ForeignKeyConstraint(["correction_of_receipt_id"], ["procurement.receipt_document.id"],
                             name="fk_receipt_document_correction_parent"),
        Index("ix_receipt_document_correction_parent", "correction_of_receipt_id", "correction_of_version"),
        Index("ix_receipt_document_correction_entry", "correction_of_entry_id"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    source_key: Mapped[str] = mapped_column(String(160))
    current_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    correction_of_receipt_id: Mapped[int | None] = mapped_column(Integer)
    correction_of_version: Mapped[int | None] = mapped_column(Integer)
    correction_of_entry_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounting.entry.id"))


class ReceiptRevision(Base):
    __tablename__ = "receipt_revision"
    __table_args__ = (
        UniqueConstraint("receipt_id", "version"),
        UniqueConstraint("receipt_id", "request_key", name="uq_receipt_revision_request"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("procurement.receipt_document.id"))
    version: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str | None] = mapped_column(String(160))
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
    correction_of = None
    if row.correction_of_receipt_id is not None:
        correction_of = {"receipt_id": row.correction_of_receipt_id,
                         "version": row.correction_of_version,
                         "entry_id": row.correction_of_entry_id}
    return {"id": row.id, "organization_id": row.organization_id, "key": row.source_key,
            "status": "posted" if posted else row.status, "version": row.current_version,
            "correction_of": correction_of,
            "posting": {"entry_id": posted.entry_id, "version": posted.version} if posted else None,
            "revisions": [{"version": r.version, "request_key": r.request_key, "document": r.document,
                           "actor": r.actor, "created_at": r.created_at} for r in revisions]}


def correction_document(document):
    if not isinstance(document, dict):
        raise HTTPException(409, "Correction source revision is incomplete or invalid")
    try:
        return ReceiptContent.model_validate(document)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "Correction source revision is incomplete or invalid") from exc


def correction_line_identity(item):
    return (item.order_id, item.order_line_id, item.sku_id, item.sku, item.sku_title,
            item.unit, item.lot)


def correction_line_values_changed(original_items, corrected_items, fields):
    original_values = Counter(tuple(getattr(item, field) for field in fields) for item in original_items)
    corrected_values = Counter(tuple(getattr(item, field) for field in fields) for item in corrected_items)
    if original_values != corrected_values:
        return True
    grouped = []
    for items in (original_items, corrected_items):
        values_by_identity = {}
        for item in items:
            identity = correction_line_identity(item)
            values_by_identity.setdefault(identity, Counter()).update([
                tuple(getattr(item, field) for field in fields),
            ])
        grouped.append(values_by_identity)
    original_by_identity, corrected_by_identity = grouped
    return any(
        original_by_identity[identity] != corrected_by_identity[identity]
        for identity in original_by_identity.keys() & corrected_by_identity.keys()
    )


def classify_receipt_correction(original, corrected):
    physical_change = (
        original.warehouse != corrected.warehouse or original.warehouse_id != corrected.warehouse_id
        or Counter(correction_line_identity(item) + (item.quantity,) for item in original.items)
        != Counter(correction_line_identity(item) + (item.quantity,) for item in corrected.items)
    )
    inventory_value_change = correction_line_values_changed(
        original.items, corrected.items, ("net_amount",),
    )
    vat_change = correction_line_values_changed(
        original.items, corrected.items, ("vat_rate", "vat_amount", "vat_basis"),
    )
    primary_document_change = any(
        getattr(original, field) != getattr(corrected, field)
        for field in ("invoice_reference", "document_date", "supplier", "supplier_id", "supplier_unp",
                      "contract", "contract_id", "contract_basis", "warehouse", "warehouse_id",
                      "warehouse_title")
    )
    operation_date_change = original.operation_date != corrected.operation_date
    explanation_only = not any((physical_change, inventory_value_change, vat_change,
                                primary_document_change, operation_date_change)) and (
        original.explanation != corrected.explanation
    )
    no_difference = not any((physical_change, inventory_value_change, vat_change,
                             primary_document_change, operation_date_change, explanation_only))
    flags = {
        "physical_change": physical_change,
        "inventory_value_change": inventory_value_change,
        "vat_change": vat_change,
        "primary_document_change": primary_document_change,
        "operation_date_change": operation_date_change,
        "explanation_only": explanation_only,
        "no_difference": no_difference,
    }
    classification = [name for name, changed in flags.items() if changed]
    return flags, classification


@router.get("/receipt-organizations")
async def organizations(ctx=Depends(context)):
    return await ctx[1].source_organizations(ctx[0], ctx[2])


@router.get("/organizations/{org_id}/receipt-warehouse-options")
async def receipt_warehouse_options(org_id: int, response: Response,
                                    q: str = Query("", max_length=100), ctx=Depends(context)):
    response.headers["Cache-Control"] = "private, no-store"
    return await ctx[1].source_warehouse_options(ctx[0], org_id, ctx[2], q)


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
        if existing.correction_of_receipt_id is not None:
            raise HTTPException(409, "Source key already identifies a correction draft")
        first = await session.scalar(select(ReceiptRevision).where(
            ReceiptRevision.receipt_id == existing.id, ReceiptRevision.version == 1,
        ))
        if ReceiptContent.model_validate(first.document).model_dump(mode="json") != payload:
            raise HTTPException(409, "Source key already identifies a different receipt")
        return await output(session, existing)
    await validate_supplier(session, data.document)
    await validate_contract(session, org_id, data.document)
    await validate_warehouse(session, org_id, data.document, access[1])
    await validate_skus(session, data.document)
    await validate_orders(session, org_id, data.document)
    row = ReceiptDocument(organization_id=org_id, source_key=data.key, current_version=1,
                          status="draft", created_by=actor)
    session.add(row)
    await session.flush()
    session.add(ReceiptRevision(receipt_id=row.id, version=1, document=payload, actor=actor))
    await access[1].source_changed(session, org_id, access[2], f"procurement:receipt:{row.id}", 1, payload["operation_date"])
    await session.flush()
    return await output(session, row)


@router.post("/organizations/{org_id}/receipt-documents/{receipt_id}/correction-drafts", status_code=201)
async def create_correction_draft(org_id: int, receipt_id: int, data: ReceiptCorrectionDraft,
                                  ctx=Depends(scoped)):
    session, actor = ctx
    original = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == org_id,
    ).with_for_update())
    if original is None:
        raise HTTPException(404, "Receipt not found")

    replay = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.organization_id == org_id, ReceiptDocument.source_key == data.key,
    ).with_for_update())
    if replay is not None:
        expected = (receipt_id, data.expected_version, data.expected_entry_id)
        actual = (replay.correction_of_receipt_id, replay.correction_of_version,
                  replay.correction_of_entry_id)
        if actual != expected:
            raise HTTPException(409, "Correction key already identifies a different source")
        return await output(session, replay)

    posting = await session.get(ReceiptPosting, receipt_id)
    if (posting is None or original.current_version != data.expected_version
            or posting.version != data.expected_version or posting.entry_id != data.expected_entry_id):
        raise HTTPException(409, "Correction requires the exact current posted receipt revision and entry")

    from modules.accounting.models import Entry

    entry = await session.get(Entry, posting.entry_id)
    if (entry is None or entry.organization_id != org_id
            or entry.source != f"procurement:receipt:{receipt_id}"
            or entry.source_version != posting.version or entry.operation != "inventory_purchase"):
        raise HTTPException(409, "Posted receipt ledger identity is inconsistent")
    revision = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == receipt_id, ReceiptRevision.version == posting.version,
    ))
    if revision is None:
        raise HTTPException(409, "Posted receipt revision is missing")

    row = ReceiptDocument(organization_id=org_id, source_key=data.key, current_version=1,
                          status="draft", created_by=actor,
                          correction_of_receipt_id=receipt_id,
                          correction_of_version=posting.version,
                          correction_of_entry_id=posting.entry_id)
    session.add(row)
    await session.flush()
    session.add(ReceiptRevision(receipt_id=row.id, version=1, document=revision.document, actor=actor))
    await session.flush()
    return await output(session, row)


@router.get("/organizations/{org_id}/receipt-documents/{receipt_id}/correction-readiness")
async def correction_readiness(org_id: int, receipt_id: int, ctx=Depends(scoped)):
    session, _ = ctx
    correction = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == org_id,
    ))
    if correction is None:
        raise HTTPException(404, "Receipt not found")
    if (correction.status != "draft" or correction.correction_of_receipt_id is None
            or correction.correction_of_version is None or correction.correction_of_entry_id is None
            or correction.correction_of_receipt_id == correction.id
            or type(correction.current_version) is not int or correction.current_version < 1):
        raise HTTPException(409, "Receipt is not a valid correction draft")
    if await session.get(ReceiptPosting, correction.id) is not None:
        raise HTTPException(409, "A correction draft cannot have a posting")

    original = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == correction.correction_of_receipt_id,
        ReceiptDocument.organization_id == org_id,
    ))
    posting = await session.get(ReceiptPosting, correction.correction_of_receipt_id)
    if (original is None or original.correction_of_receipt_id is not None or posting is None
            or original.current_version != correction.correction_of_version
            or posting.version != correction.correction_of_version
            or posting.entry_id != correction.correction_of_entry_id):
        raise HTTPException(409, "Correction source identity is stale or inconsistent")

    from modules.accounting.models import Entry

    entry = await session.get(Entry, correction.correction_of_entry_id)
    if (entry is None or entry.organization_id != org_id
            or entry.source != f"procurement:receipt:{original.id}"
            or entry.source_version != correction.correction_of_version
            or entry.operation != "inventory_purchase"):
        raise HTTPException(409, "Correction source ledger identity is stale or inconsistent")

    original_revision = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == original.id,
        ReceiptRevision.version == correction.correction_of_version,
    ))
    current_revision = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == correction.id,
        ReceiptRevision.version == correction.current_version,
    ))
    if original_revision is None or current_revision is None:
        raise HTTPException(409, "Correction source or current draft revision is missing")
    original_document = correction_document(original_revision.document)
    corrected_document = correction_document(current_revision.document)
    flags, classification = classify_receipt_correction(original_document, corrected_document)

    blockers = []
    blocker_by_flag = (
        ("physical_change", "physical_receipt_adjustment_required"),
        ("inventory_value_change", "inventory_cost_layer_correction_required"),
        ("vat_change", "vat_register_and_eschf_evidence_required"),
        ("primary_document_change", "primary_document_reissue_evidence_required"),
    )
    for flag, code in blocker_by_flag:
        if flags[flag]:
            blockers.append(code)
    if any(flags[name] for name in (
        "physical_change", "inventory_value_change", "vat_change", "primary_document_change",
        "operation_date_change",
    )):
        blockers.append("correction_posting_period_required")
    if flags["explanation_only"] or flags["no_difference"]:
        blockers.append("no_economic_difference")
    blockers.append("posting_workflow_not_approved")

    correction_of = {
        "receipt_id": correction.correction_of_receipt_id,
        "version": correction.correction_of_version,
        "entry_id": correction.correction_of_entry_id,
    }
    return {
        "organization_id": org_id,
        "receipt_id": correction.id,
        "current_version": correction.current_version,
        "correction_of": correction_of,
        "classification": classification,
        **flags,
        "posting_available": False,
        "wms_movement_available": False,
        "blockers": blockers,
    }


@router.put("/organizations/{org_id}/receipt-documents/{receipt_id}")
async def edit_document(org_id: int, receipt_id: int, data: ReceiptEdit, ctx=Depends(scoped), access=Depends(context)):
    session, actor = ctx
    row = await session.scalar(select(ReceiptDocument).where(
        ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == org_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "Receipt not found")
    payload = data.document.model_dump(mode="json")
    repeated = await session.scalar(select(ReceiptRevision).where(
        ReceiptRevision.receipt_id == row.id, ReceiptRevision.request_key == data.key,
    ))
    if repeated is not None:
        if (repeated.version != data.expected_version + 1 or repeated.actor != actor
                or repeated.document != payload):
            raise HTTPException(409, "Edit key already identifies a different receipt revision")
        if row.current_version != repeated.version:
            raise HTTPException(409, "Receipt changed after the repeated edit; reopen the current version")
        return await output(session, row)
    if row.status != "draft" or row.current_version != data.expected_version or await session.get(ReceiptPosting, row.id):
        raise HTTPException(409, "Receipt is not an editable draft at the expected version")
    await validate_supplier(session, data.document)
    await validate_contract(session, org_id, data.document)
    await validate_warehouse(session, org_id, data.document, access[1])
    await validate_skus(session, data.document)
    await validate_orders(session, org_id, data.document)
    row.current_version += 1
    session.add(ReceiptRevision(receipt_id=row.id, version=row.current_version,
                                request_key=data.key, document=payload, actor=actor))
    if row.correction_of_receipt_id is None:
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


async def validate_contract(session, org_id, document):
    if document.contract_id is None:
        if document.contract_basis is None:
            raise HTTPException(422, "Select a registered supplier contract or explain the unregistered basis")
        return None
    from modules.procurement.supplier_contracts import SupplierContract

    contract = await session.scalar(select(SupplierContract).where(
        SupplierContract.id == document.contract_id,
        SupplierContract.organization_id == org_id,
        SupplierContract.supplier_id == document.supplier_id,
        SupplierContract.number == document.contract,
        SupplierContract.is_active.is_(True),
        SupplierContract.signed_on <= document.document_date,
    ).with_for_update(read=True))
    if contract is None or (contract.expires_on is not None
                            and contract.expires_on < document.document_date):
        raise HTTPException(409, "Supplier contract selection changed or is outside its signed dates")
    return contract


async def validate_warehouse(session, org_id, document, accounting):
    if document.warehouse_id is None or document.warehouse_title is None:
        raise HTTPException(422, "Select the warehouse from the organization directory")
    await accounting.verify_warehouse_snapshot(session, org_id, {
        "warehouse_id": document.warehouse_id,
        "warehouse": document.warehouse,
        "warehouse_title": document.warehouse_title,
    })


async def validate_skus(session, document):
    if any(item.sku_id is None or item.sku_title is None or item.unit is None for item in document.items):
        raise HTTPException(422, "Select every receipt item from the active SKU catalogue")
    ids = {item.sku_id for item in document.items}
    rows = (await session.scalars(select(Sku).where(Sku.id.in_(ids)).with_for_update(read=True))).all()
    skus = {row.id: row for row in rows}
    if any((sku := skus.get(item.sku_id)) is None or not sku.is_active
           or sku.code != item.sku or sku.title != item.sku_title or sku.unit != item.unit
           for item in document.items):
        raise HTTPException(409, "SKU catalogue changed; select the active item again")


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
    if row.correction_of_receipt_id is not None:
        raise HTTPException(409, "Correction draft requires the separately approved correction workflow")
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
    if row.status == "draft" and facts.get("contract_id") is None and facts.get("contract_basis") is None:
        raise HTTPException(409, "Match the draft contract or record the unregistered basis before posting")
    if row.status == "draft" and (facts.get("warehouse_id") is None or facts.get("warehouse_title") is None):
        raise HTTPException(409, "Match the draft warehouse to the organization directory before posting")
    if row.status == "draft" and any(
        item.get("sku_id") is None or item.get("sku_title") is None or item.get("unit") is None
        for item in facts["items"]
    ):
        raise HTTPException(409, "Match every draft receipt item to the SKU catalogue before posting")
    verified_counterparty_id = frozen_counterparty_id
    if require_current_supplier:
        source_document = ReceiptContent.model_validate(facts)
        supplier = await validate_supplier(session, source_document)
        await validate_contract(session, org_id, source_document)
        verified_counterparty_id = supplier.counterparty_id
    if len(data.inventory_accounts) != len(facts["items"]):
        raise HTTPException(422, "Select an inventory account for every source line")
    document = {**{k: v for k, v in facts.items() if k not in {"currency", "supplier", "supplier_id", "supplier_unp", "contract_id", "contract_basis", "items"}},
            "source": f"procurement:receipt:{row.id}", "source_version": row.current_version,
            "counterparty": facts["supplier"], "posting_date": data.posting_date.isoformat(),
            "policy_id": data.policy_id, "settlement_account": data.settlement_account,
            "vat_account": data.vat_account,
            "items": [{**{k: v for k, v in item.items() if k not in {"order_line_id", "sku_id", "sku_title"}}, "account": account} for item, account in zip(facts["items"], data.inventory_accounts, strict=True)]}
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
