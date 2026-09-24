"""Organization-owned operational reads; customer requirements remain unverified."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from core.domain.models import Sku
from modules.procurement.expected_reservations import PhysicalReceiptAcceptance
from modules.procurement.models import (
    OPEN_ORDER_STATUSES,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Supplier,
)
from modules.procurement.ownership import (
    OrderRequestLink,
    PurchaseOwnership,
    current_read_scope,
    owned_plan_source,
)
from modules.procurement.receipt_documents import ReceiptDocument, ReceiptPosting, ReceiptRevision
from modules.procurement.routes import _order_milestones, order_landed_preview, owned_plan_out
from modules.procurement.schemas import ScopedOrderPlanOut

router = APIRouter(tags=["Organization procurement reads"])


@router.get("/organizations/{org_id}/sku-options")
async def sku_options(org_id: int, q: str = Query("", max_length=100),
                      ctx=Depends(current_read_scope)):
    session, _ = ctx
    statement = select(Sku).where(Sku.is_active.is_(True))
    if q.strip():
        term = q.strip()
        statement = statement.where(or_(Sku.code.icontains(term, autoescape=True),
                                        Sku.title.icontains(term, autoescape=True)))
    rows = (await session.scalars(statement.order_by(Sku.code).limit(51))).all()
    return {"organization_id": org_id,
            "items": [{"id": row.id, "code": row.code, "title": row.title,
                       "unit": row.unit} for row in rows[:50]],
            "truncated": len(rows) > 50}


@router.get("/organizations/{org_id}/supplier-options")
async def supplier_options(org_id: int, q: str = Query("", max_length=100),
                           ctx=Depends(current_read_scope)):
    session, _ = ctx
    statement = select(Supplier).where(Supplier.status == "active")
    if q.strip():
        term = q.strip()
        statement = statement.where(or_(Supplier.name.icontains(term, autoescape=True),
                                        Supplier.unp.icontains(term, autoescape=True)))
    rows = (await session.scalars(statement.order_by(Supplier.name, Supplier.id).limit(51))).all()
    return {"organization_id": org_id,
            "items": [{"id": row.id, "name": row.name, "unp": row.unp} for row in rows[:50]],
            "truncated": len(rows) > 50}


@router.get("/organizations/{org_id}/open-orders")
async def open_orders(org_id: int, after_id: int = Query(0, ge=0), ctx=Depends(current_read_scope)):
    session, _ = ctx
    # Stable ID keyset; ETA is data, not an ambiguous offset cursor.
    rows = (await session.scalars(select(PurchaseOrder).join(PurchaseOwnership,
        (PurchaseOwnership.kind == "order") & (PurchaseOwnership.source_id == PurchaseOrder.id))
        .where(PurchaseOwnership.organization_id == org_id,
               PurchaseOrder.status.in_(OPEN_ORDER_STATUSES), PurchaseOrder.id > after_id)
        .order_by(PurchaseOrder.id).limit(51))).all()
    page = rows[:50]
    return {"organization_id": org_id, "items": [{"id": r.id, "number": r.number,
        "supplier": r.supplier, "status": r.status, "eta_date": r.eta_date,
        "freight_byn": str(r.freight_byn)} for r in page],
        "next_after_id": page[-1].id if len(rows) > 50 else None}


@router.get("/organizations/{org_id}/orders/{order_id}/landed-preview")
async def preview(org_id: int, order_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    _, order = await owned_plan_source(session, org_id, "order", order_id)
    return {"organization_id": org_id, **await order_landed_preview(session, order)}


@router.get("/organizations/{org_id}/orders/{order_id}/plan", response_model=ScopedOrderPlanOut)
async def plan(org_id: int, order_id: int, ctx=Depends(current_read_scope)):
    session, _ = ctx
    _, order = await owned_plan_source(session, org_id, "order", order_id)
    return owned_plan_out(org_id, order, await _order_milestones(session, order_id),
                          datetime.now(timezone.utc).date())


def _decimal_text(value, *, default="0.00"):
    """Normalize quantities for a read projection without turning bad data into zero."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return format(result, ".2f")


async def _receipt_chain(session, org_id: int, order_id: int) -> list[dict]:
    """Return primary incoming documents bound to one owned supplier order.

    Procurement owns the primary-document and acceptance projections.  The endpoint is
    intentionally read-only: it never guesses an invoice, accounting posting, or QC
    result when the corresponding immutable source is absent.
    """
    documents = (await session.scalars(select(ReceiptDocument).where(
        ReceiptDocument.organization_id == org_id,
    ).order_by(ReceiptDocument.id))).all()
    result = []
    for document in documents:
        posting = await session.get(ReceiptPosting, document.id)
        version = posting.version if posting is not None else document.current_version
        revision = await session.scalar(select(ReceiptRevision).where(
            ReceiptRevision.receipt_id == document.id,
            ReceiptRevision.version == version,
        ))
        if revision is None or not isinstance(revision.document, dict):
            # A broken source is visible as an unresolved chain item only when it can be
            # associated with this order; unrelated corrupt history stays out of the view.
            continue
        facts = revision.document
        raw_items = facts.get("items")
        if not isinstance(raw_items, list):
            continue
        items = [item for item in raw_items if isinstance(item, dict) and item.get("order_id") == order_id]
        if not items:
            continue
        accepted = (await session.scalars(select(PhysicalReceiptAcceptance).where(
            PhysicalReceiptAcceptance.organization_id == org_id,
            PhysicalReceiptAcceptance.source_receipt_id == document.id,
        ).order_by(PhysicalReceiptAcceptance.receipt_id))).all()
        result.append({
            "id": document.id,
            "source_key": document.source_key,
            "version": version,
            "status": "posted" if posting is not None else document.status,
            "posting": ({"entry_id": posting.entry_id, "version": posting.version}
                         if posting is not None else None),
            "invoice_reference": facts.get("invoice_reference"),
            "document_date": facts.get("document_date"),
            "operation_date": facts.get("operation_date"),
            "supplier": facts.get("supplier"),
            "contract": facts.get("contract"),
            "warehouse": facts.get("warehouse"),
            "lines": [{
                "order_line_id": item.get("order_line_id"),
                "sku": item.get("sku"),
                "lot": item.get("lot"),
                "quantity": _decimal_text(item.get("quantity")),
                "unit": item.get("unit"),
            } for item in items],
            "physical_acceptance": [{
                "receipt_id": row.receipt_id,
                "source_version": row.source_version,
                "lines": row.lines,
                "evidence": row.evidence,
                "actor": row.actor,
                "created_at": row.created_at,
            } for row in accepted],
        })
    return result


@router.get("/organizations/{org_id}/orders/{order_id}/chain")
async def purchase_chain(org_id: int, order_id: int, ctx=Depends(current_read_scope)):
    """Show the verifiable procurement chain for one organization-owned order.

    This is a read model for the buyer/accountant workspace.  Every stage carries its
    own evidence; missing stages are blockers and never inferred from a neighbouring
    document or from a payment.
    """
    session, _ = ctx
    owner, order = await owned_plan_source(session, org_id, "order", order_id)
    lines = (await session.scalars(select(PurchaseOrderLine).where(
        PurchaseOrderLine.order_id == order.id,
    ).order_by(PurchaseOrderLine.id))).all()
    links = (await session.scalars(select(OrderRequestLink).where(
        OrderRequestLink.organization_id == org_id,
        OrderRequestLink.order_ownership_id == owner.id,
    ).order_by(OrderRequestLink.id))).all()
    requests = []
    for link in links:
        request_owner = await session.get(PurchaseOwnership, link.request_ownership_id)
        if request_owner is None or request_owner.organization_id != org_id or request_owner.kind != "request":
            continue
        request = await session.get(PurchaseRequest, request_owner.source_id)
        if request is None:
            continue
        requests.append({
            "id": link.id,
            "request_id": request.id,
            "ownership_id": request_owner.id,
            "number": request.number,
            "supplier": request.supplier,
            "item": request.item,
            "quantity": str(request.qty),
            "planned_amount": str(request.amount),
            "stage": request.stage,
            "evidence": link.evidence,
        })
    receipts = await _receipt_chain(session, org_id, order.id)
    blockers = []
    if not requests:
        blockers.append("purchase_request_not_linked")
    if not receipts:
        blockers.append("incoming_invoice_not_registered")
    elif any(receipt["posting"] is None for receipt in receipts):
        blockers.append("incoming_invoice_not_posted")
    if receipts and any(not receipt["physical_acceptance"] for receipt in receipts):
        blockers.append("warehouse_receipt_not_accepted")
    return {
        "organization_id": org_id,
        "order": {
            "id": order.id,
            "number": order.number,
            "supplier": order.supplier,
            "status": order.status,
            "eta_date": order.eta_date,
            "freight_byn": str(order.freight_byn),
            "lines": [{
                "id": line.id,
                "sku_code": line.sku_code,
                "quantity": str(line.qty),
                "goods_value_byn": str(line.goods_value_byn),
            } for line in lines],
        },
        "request_links": requests,
        "receipts": receipts,
        "stages": {
            "request": "linked" if requests else "missing",
            "order": "owned",
            "incoming_invoice": "posted" if receipts and all(row["posting"] for row in receipts) else "draft" if receipts else "missing",
            "warehouse": "accepted" if receipts and all(row["physical_acceptance"] for row in receipts) else "pending" if receipts else "missing",
        },
        "status": "complete" if not blockers else "partial",
        "blockers": blockers,
    }


@router.get("/organizations/{org_id}/orders/{order_id}/customer-deadlines")
async def customer_deadlines(org_id: int, order_id: int, ctx=Depends(current_read_scope)):
    from modules.procurement.customer_deadlines import review

    session, _ = ctx
    _, order = await owned_plan_source(session, org_id, "order", order_id)
    return await review(session, org_id, order)
