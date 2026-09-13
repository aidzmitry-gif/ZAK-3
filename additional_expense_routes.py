"""Company-scoped additional-expense primary documents; no posting yet."""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select

from modules.procurement.additional_expenses import (
    AdditionalExpenseCreate,
    AdditionalExpenseDocument,
    AdditionalExpenseEdit,
    AdditionalExpenseRevision,
    save_document,
)
from modules.procurement.ownership import effective_plan_user, plan_context

router = APIRouter(tags=["Дополнительные расходы закупок"])


def response(data):
    return JSONResponse(jsonable_encoder(data), headers={"Cache-Control": "private, no-store"})


async def reader(org_id: int, ctx=Depends(plan_context)):
    session, gateway, user = ctx
    await gateway.source_member(session, org_id, user)
    user = await effective_plan_user(session, user)
    await gateway.source_member(session, org_id, user)
    return session, gateway, user


async def writer(org_id: int, x_expected_principal: str = Header(min_length=1), ctx=Depends(reader)):
    session, gateway, user = ctx
    actor = await gateway.source_write_authority(session, org_id, user)
    if actor != x_expected_principal:
        raise HTTPException(409, "Current principal differs from the saved command")
    return ctx


@router.get("/organizations/{org_id}/additional-expense-context")
async def document_context(org_id: int, ctx=Depends(reader)):
    session, gateway, user = ctx
    actor = await gateway.source_member(session, org_id, user)
    can_write = True
    try:
        await gateway.source_write_authority(session, org_id, user)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        can_write = False
    return response({"organization_id": org_id, "principal": actor, "can_write": can_write})


async def output(session, row, gateway, user):
    revisions = list((await session.scalars(select(AdditionalExpenseRevision).where(
        AdditionalExpenseRevision.expense_id == row.id).order_by(AdditionalExpenseRevision.version))).all())
    if not revisions:
        raise HTTPException(409, "Additional expense history is missing")
    posting = await gateway.additional_expense_status(session, row.organization_id, user, row.id)
    if posting is not None and posting["version"] != revisions[-1].version:
        raise HTTPException(409, "Additional expense posting version is inconsistent")
    return {"id": row.id, "organization_id": row.organization_id, "key": row.source_key,
            "version": revisions[-1].version, "status": "posted" if posting else "draft", "posted": posting is not None,
            "posting": posting,
            "revisions": [{"version": r.version, "document": r.document, "receipt_sources": r.receipt_sources,
                           "actor": r.actor, "created_at": r.created_at} for r in revisions]}


@router.get("/organizations/{org_id}/additional-expenses")
async def list_documents(org_id: int, limit: int = Query(default=100, ge=1, le=200),
                         offset: int = Query(default=0, ge=0), ctx=Depends(reader)):
    rows = (await ctx[0].scalars(select(AdditionalExpenseDocument).where(
        AdditionalExpenseDocument.organization_id == org_id).order_by(AdditionalExpenseDocument.id.desc())
        .limit(limit).offset(offset))).all()
    return response([await output(ctx[0], row, ctx[1], ctx[2]) for row in rows])


@router.get("/organizations/{org_id}/additional-expenses/{expense_id}")
async def get_document(org_id: int, expense_id: int, ctx=Depends(reader)):
    row = await ctx[0].scalar(select(AdditionalExpenseDocument).where(
        AdditionalExpenseDocument.organization_id == org_id, AdditionalExpenseDocument.id == expense_id))
    if row is None:
        raise HTTPException(404, "Additional expense not found")
    return response(await output(ctx[0], row, ctx[1], ctx[2]))


@router.post("/organizations/{org_id}/additional-expenses", status_code=201)
async def create_document(org_id: int, data: AdditionalExpenseCreate, ctx=Depends(writer)):
    row, _ = await save_document(*ctx, org_id, data)
    result = response(await output(ctx[0], row, ctx[1], ctx[2]))
    result.status_code = 201
    return result


@router.put("/organizations/{org_id}/additional-expenses/{expense_id}")
async def edit_document(org_id: int, expense_id: int, data: AdditionalExpenseEdit, ctx=Depends(writer)):
    row, _ = await save_document(*ctx, org_id, data, expense_id=expense_id)
    return response(await output(ctx[0], row, ctx[1], ctx[2]))
