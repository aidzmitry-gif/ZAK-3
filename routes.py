"""HTTP-API модуля Procurement. Монтируется под префиксом ``/procurement``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.procurement.models import PurchaseRequest
from modules.procurement.schemas import PurchaseRequestCreate, PurchaseRequestOut, StatusUpdate

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


@router.patch("/requests/{req_id}", response_model=PurchaseRequestOut)
async def update_request(
    req_id: int,
    payload: StatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить статус заявки. При ``received`` — приход на склад (procurement → wms)."""
    obj = await session.get(PurchaseRequest, req_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    obj.status = payload.status
    if payload.status == "received":
        core.event_bus.emit(
            session,
            "procurement.received",
            {"item": obj.item, "qty": obj.qty, "warehouse": "Главный", "entity_ref": f"purchase:{obj.id}"},
        )
    await session.commit()
    await session.refresh(obj)
    return obj
