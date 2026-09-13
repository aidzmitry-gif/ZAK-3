"""Primary additional-expense inputs and exact posted-receipt provenance.

These inputs do not calculate VAT, convert currency or create ledger movements.
"""
from datetime import date, datetime

from fastapi import HTTPException
from pydantic import Field, model_validator
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
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.procurement.receipt_documents import (
    Input,
    Money,
    ReceiptContent,
    ReceiptDocument,
    ReceiptPosting,
    ReceiptRevision,
    immutable,
)


class ExpenseReceiptLine(Input):
    receipt_id: int = Field(gt=0, strict=True)
    version: int = Field(gt=0, strict=True)
    line_number: int = Field(gt=0, strict=True)


class AdditionalExpenseContent(Input):
    invoice_reference: str = Field(min_length=1, max_length=200)
    document_date: date
    operation_date: date
    supplier: str = Field(min_length=1, max_length=200)
    contract: str = Field(min_length=1, max_length=200)
    # Original-document currency only; recognition requires a validated rate/policy.
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Money = Field(gt=0)
    explanation: str = Field(min_length=1, max_length=700)
    receipt_lines: list[ExpenseReceiptLine] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def unique_lines(self):
        keys = [(line.receipt_id, line.version, line.line_number) for line in self.receipt_lines]
        if len(keys) != len(set(keys)):
            raise ValueError("A receipt line may be linked only once")
        return self


class AdditionalExpenseCreate(Input):
    key: str = Field(min_length=1, max_length=160)
    document: AdditionalExpenseContent


class AdditionalExpenseEdit(Input):
    expected_version: int = Field(ge=1, strict=True)
    document: AdditionalExpenseContent


class AdditionalExpenseDocument(Base):
    __tablename__ = "additional_expense_document"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    source_key: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdditionalExpenseRevision(Base):
    __tablename__ = "additional_expense_revision"
    __table_args__ = (UniqueConstraint("expense_id", "version"), {"schema": "procurement"})
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("procurement.additional_expense_document.id"))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    receipt_sources: Mapped[list] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


for model in (AdditionalExpenseDocument, AdditionalExpenseRevision):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)


async def save_document(session, gateway, user, organization_id, data, *, expense_id=None):
    """Append one source revision; transaction/commit belongs to the caller.

    Organization authority serializes writes with accounting period closure.
    Headers and revisions are immutable; the current version is the maximum.
    """
    actor = await gateway.source_write_authority(session, organization_id, user)
    if expense_id is None:
        if not isinstance(data, AdditionalExpenseCreate):
            raise ValueError("Create input required")
        row = await session.scalar(select(AdditionalExpenseDocument).where(
            AdditionalExpenseDocument.organization_id == organization_id,
            AdditionalExpenseDocument.source_key == data.key))
    else:
        if not isinstance(data, AdditionalExpenseEdit):
            raise ValueError("Edit input required")
        row = await session.scalar(select(AdditionalExpenseDocument).where(
            AdditionalExpenseDocument.organization_id == organization_id,
            AdditionalExpenseDocument.id == expense_id))
        if row is None:
            raise HTTPException(404, "Additional expense not found")
    payload = data.document.model_dump(mode="json")
    revisions = [] if row is None else list((await session.scalars(select(AdditionalExpenseRevision).where(
        AdditionalExpenseRevision.expense_id == row.id).order_by(AdditionalExpenseRevision.version))).all())
    if row is not None and not revisions:
        raise HTTPException(409, "Additional expense source history is missing")
    if expense_id is None and row is not None:
        if revisions[0].document != payload:
            raise HTTPException(409, "Source key already identifies a different additional expense")
        return row, revisions[-1]
    if expense_id is not None and revisions[-1].version != data.expected_version:
        repeated = next((revision for revision in revisions if revision.version == data.expected_version + 1), None)
        if repeated is not None and repeated.document == payload and repeated.actor == actor:
            return row, revisions[-1]
        raise HTTPException(409, "Additional expense version changed; reload the document")
    if expense_id is not None and await gateway.additional_expense_status(session, organization_id, user, expense_id) is not None:
        raise HTTPException(409, "Posted additional expense requires a separate correction workflow")
    sources = await resolve_receipt_lines(session, organization_id, data.document)
    if row is None:
        row = AdditionalExpenseDocument(organization_id=organization_id, source_key=data.key, created_by=actor)
        session.add(row)
        await session.flush()
    version = revisions[-1].version + 1 if revisions else 1
    await gateway.source_changed(session, organization_id, user, f"procurement:additional-expense:{row.id}",
                                 version, data.document.operation_date.isoformat())
    revision = AdditionalExpenseRevision(expense_id=row.id, version=version, document=payload,
                                         receipt_sources=sources, actor=actor)
    session.add(revision)
    await session.flush()
    return row, revision


async def resolve_receipt_lines(session, organization_id: int, document: AdditionalExpenseContent):
    """Return source snapshots in input order, scoped to one already-authorized book.

    The caller must authorize organization access. No SKU/latest-cost fallback is
    allowed. Immutable posting/revision rows are the source of line identity.
    Ledger authenticity and valuation remain the accounting gateway's concern.
    """
    ids = {line.receipt_id for line in document.receipt_lines}
    rows = (await session.execute(select(ReceiptDocument, ReceiptPosting, ReceiptRevision).join(
        ReceiptPosting, ReceiptPosting.receipt_id == ReceiptDocument.id,
    ).join(ReceiptRevision, (ReceiptRevision.receipt_id == ReceiptPosting.receipt_id)
           & (ReceiptRevision.version == ReceiptPosting.version)).where(
        ReceiptDocument.organization_id == organization_id, ReceiptDocument.id.in_(ids),
    ))).all()
    sources = {receipt.id: (posting, revision) for receipt, posting, revision in rows}
    if set(sources) != ids:
        raise ValueError("Every receipt must be posted in this organization with its source revision")
    result = []
    for link in document.receipt_lines:
        posting, revision = sources[link.receipt_id]
        if posting.version != link.version:
            raise ValueError("Receipt reference must identify its exact posted version")
        source = ReceiptContent.model_validate(revision.document)
        if link.line_number > len(source.items):
            raise ValueError("Receipt line does not exist in the posted version")
        result.append({**link.model_dump(), "entry_id": posting.entry_id,
                       "posting_digest": posting.digest,
                       "document_date": source.document_date.isoformat(),
                       "operation_date": source.operation_date.isoformat(),
                       "currency": source.currency, "warehouse": source.warehouse,
                       "item": source.items[link.line_number - 1].model_dump(mode="json")})
    return result
