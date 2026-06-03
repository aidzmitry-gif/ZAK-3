"""HTTP-API модуля Procurement. Монтируется под префиксом ``/procurement``."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from modules.procurement.models import PurchaseRequest
from modules.procurement.schemas import PurchaseRequestCreate, PurchaseRequestOut

router = APIRouter(tags=["procurement"])


@router.get("/requests", response_model=list[PurchaseRequestOut])
async def list_requests(session: AsyncSession = Depends(get_session)):
    """Заявки на закупку."""
    return (
        await session.execute(select(PurchaseRequest).order_by(PurchaseRequest.id.desc()))
    ).scalars().all()


@router.post("/requests", response_model=PurchaseRequestOut, status_code=201)
async def create_request(
    payload: PurchaseRequestCreate, session: AsyncSession = Depends(get_session)
):
    """Создать заявку на закупку."""
    obj = PurchaseRequest(**payload.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj
