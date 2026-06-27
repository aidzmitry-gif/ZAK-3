"""HTTP-API модуля Procurement. Монтируется под префиксом ``/procurement``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from core.services.landed_cost import LandedExpense, LandedLine, allocate_landed_cost
from modules.procurement.cost_estimate import CostLine, CostRates, estimate_china_cost
from modules.procurement.models import (
    OPEN_ORDER_STATUSES,
    ORDER_RANK,
    RECEIVED_ORDER_STATUS,
    LandedCost,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Rfq,
    RfqBid,
    Supplier,
    SupplierClaim,
)
from modules.procurement.schemas import (
    CostEstimateOut,
    CostEstimateRequest,
    PurchaseOrderCreate,
    PurchaseOrderHeaderUpdate,
    PurchaseOrderLineIn,
    PurchaseOrderLineOut,
    PurchaseOrderOut,
    PurchaseOrderStatusUpdate,
    PurchaseRequestCreate,
    PurchaseRequestOut,
    RfqAward,
    RfqBidIn,
    RfqBidOut,
    RfqCreate,
    RfqOut,
    StageUpdate,
    SupplierClaimCreate,
    SupplierClaimOut,
    SupplierClaimUpdate,
    SupplierCreate,
    SupplierOut,
    SupplierUpdate,
)
from modules.procurement.stages import STAGES

router = APIRouter(tags=["procurement"])

# Переход в эту стадию воронки = товар физически принят → приход на склад (procurement → wms).
RECEIVED_STAGE = "qc"
CLAIM_CLOSED_STATUSES = ("resolved", "rejected")


# ───────────────────────── Скоринг поставщика (чистая функция) ─────────────────────────


def _score_components(orders_count: int, claims_total: int, on_time_rate: float | None) -> dict:
    """Компоненты скоринга поставщика (0..1) + итоговый балл 0..10.

    ``quality`` = 1 − доля претензий (на заказ); ``timeliness`` = доля вовремя (None — нет дат
    факта приёмки → honest-empty); ``price`` = None (нет эталона для нормировки). Балл —
    взвешенное среднее доступных компонент × 10.
    # ponytail: веса захардкожены (quality 0.6 / timeliness 0.4); апгрейд — настройка весов.
    """
    if orders_count:
        quality: float | None = 1.0 - min(1.0, claims_total / orders_count)
    else:
        quality = 0.0 if claims_total else None
    components = {"quality": quality, "timeliness": on_time_rate, "price": None}
    avail = [(w, v) for w, v in ((0.6, quality), (0.4, on_time_rate)) if v is not None]
    if not avail:
        return {"components": components, "score": None}
    score = sum(w * v for w, v in avail) / sum(w for w, _ in avail) * 10
    return {"components": components, "score": round(score, 1)}


async def _board_scores(session: AsyncSession, supplier_ids: set[int]) -> dict[int, float | None]:
    """Балл поставщика для карточек воронки (батч, без N+1): по заказам и претензиям."""
    if not supplier_ids:
        return {}
    orders = dict(
        (await session.execute(
            select(PurchaseOrder.supplier_id, func.count())
            .where(PurchaseOrder.supplier_id.in_(supplier_ids))
            .group_by(PurchaseOrder.supplier_id)
        )).all()
    )
    claims = dict(
        (await session.execute(
            select(SupplierClaim.supplier_id, func.count())
            .where(SupplierClaim.supplier_id.in_(supplier_ids))
            .group_by(SupplierClaim.supplier_id)
        )).all()
    )
    return {
        sid: _score_components(int(orders.get(sid, 0)), int(claims.get(sid, 0)), None)["score"]
        for sid in supplier_ids
    }


def _to_card(r: PurchaseRequest, score: float | None = None) -> FunnelCard:
    # Реальный балл поставщика (если связан supplier_id и есть данные), иначе пусто
    score_txt = f"Score {score}" if score is not None else ""
    return FunnelCard(
        id=r.id,
        code=r.number or f"ЗАК-{r.id}",
        title=r.item,
        subtitle=r.supplier,
        flag=r.flag,
        amount=float(r.amount),
        priority=r.priority,
        owner=r.owner,
        date=r.due_date or "",
        insight=r.insight,
        score=score_txt,
        tags=[f"{r.qty} шт"] if r.qty else [],
    )


# ───────────────────────── Воронка закупок (PurchaseRequest) ─────────────────────────


@router.get("/requests", response_model=list[PurchaseRequestOut])
async def list_requests(session: AsyncSession = Depends(get_session)):
    """Заявки на закупку (плоский список — для аналитики и совместимости)."""
    return (
        await session.execute(select(PurchaseRequest).order_by(PurchaseRequest.id.desc()))
    ).scalars().all()


@router.get("/board", response_model=FunnelBoardOut)
async def board(session: AsyncSession = Depends(get_session)) -> FunnelBoardOut:
    """Воронка закупок: заявки сгруппированы по стадиям sourcing-цикла (балл поставщика — реальный)."""
    rows = (await session.execute(select(PurchaseRequest))).scalars().all()
    scores = await _board_scores(session, {r.supplier_id for r in rows if r.supplier_id})
    return build_board(STAGES, rows, lambda r: _to_card(r, scores.get(r.supplier_id)))


@router.post("/requests", response_model=PurchaseRequestOut, status_code=201)
async def create_request(
    payload: PurchaseRequestCreate, session: AsyncSession = Depends(get_session)
):
    """Создать закупку. Номер генерируется автоматически, если не задан."""
    data = payload.model_dump()
    data["amount"] = Decimal(str(data["amount"]))
    obj = PurchaseRequest(**data)
    session.add(obj)
    await session.flush()
    if not obj.number:
        obj.number = f"ЗАК-2026-{obj.id:04d}"
    await session.commit()
    await session.refresh(obj)
    return obj


@router.patch("/requests/{req_id}", response_model=PurchaseRequestOut)
async def update_request(
    req_id: int,
    payload: StageUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить стадию закупки. При «Приёмке / QC» — приход на склад (procurement → wms)."""
    obj = await session.get(PurchaseRequest, req_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    obj.stage = payload.stage
    if payload.stage == RECEIVED_STAGE:
        core.event_bus.emit(
            session,
            "procurement.received",
            {
                "item": obj.item,
                "qty": obj.qty,
                "warehouse": "Главный",
                "entity_ref": f"purchase:{obj.id}",
            },
        )
    await session.commit()
    await session.refresh(obj)
    return obj


# ───────────────────────── Заказы (PurchaseOrder) + landed cost ─────────────────────────


async def _orders_out(session: AsyncSession, orders: list[PurchaseOrder]) -> list[PurchaseOrderOut]:
    """Собрать заказы с позициями (один запрос на все позиции — без N+1)."""
    ids = [o.id for o in orders]
    lines_by_order: dict[int, list[PurchaseOrderLine]] = {}
    if ids:
        rows = (
            await session.execute(
                select(PurchaseOrderLine)
                .where(PurchaseOrderLine.order_id.in_(ids))
                .order_by(PurchaseOrderLine.id)
            )
        ).scalars().all()
        for ln in rows:
            lines_by_order.setdefault(ln.order_id, []).append(ln)
    return [
        PurchaseOrderOut(
            id=o.id,
            number=o.number,
            supplier=o.supplier,
            supplier_id=o.supplier_id,
            status=o.status,
            eta_date=o.eta_date,
            freight_byn=float(o.freight_byn),
            lines=[PurchaseOrderLineOut.model_validate(ln) for ln in lines_by_order.get(o.id, [])],
        )
        for o in orders
    ]


def _order_allocation(lines: list[PurchaseOrderLine], freight: Decimal) -> tuple[dict, dict]:
    """Агрегировать позиции по sku_code и разнести фрахт общим движком ``allocate_landed_cost``.

    Возврат: (результат allocate_landed_cost, agg по sku). Позиции без sku_code / с qty<=0
    пропускаются (себестоимость единицы не определена — не маскируем нулём)."""
    agg: dict[str, dict[str, Decimal]] = {}
    for ln in lines:
        if not ln.sku_code or ln.qty <= 0:
            continue
        a = agg.setdefault(
            ln.sku_code,
            {"qty": Decimal("0"), "goods": Decimal("0"), "weight": Decimal("0"), "volume": Decimal("0")},
        )
        a["qty"] += Decimal(ln.qty)
        a["goods"] += Decimal(ln.goods_value_byn)
        a["weight"] += Decimal(ln.weight)
        a["volume"] += Decimal(ln.volume)
    landed_lines = [
        LandedLine(sku_code=code, qty=a["qty"], weight=a["weight"], volume=a["volume"], goods_value=a["goods"])
        for code, a in agg.items()
    ]
    expenses: list[LandedExpense] = []
    if freight and landed_lines:
        total_weight = sum((ln.weight for ln in landed_lines), Decimal("0"))
        expenses.append(LandedExpense("фрахт", freight, "weight" if total_weight > 0 else "value"))
    return allocate_landed_cost(landed_lines, expenses), agg


async def _fixate_landed_cost(session: AsyncSession, order: PurchaseOrder, event_bus) -> None:
    """На приёмке (``received``): разнести фрахт на позиции, зафиксировать себестоимость per-SKU
    (upsert), эмитить ``procurement.landed_cost.calculated`` (для sales/finance) и
    ``procurement.received`` ПО КАЖДОЙ позиции (приход на склад по себестоимости, для wms).
    Порядок: фиксация cost → события (cost уже зафиксирован)."""
    lines = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
        )
    ).scalars().all()
    res, agg = _order_allocation(lines, Decimal(order.freight_byn))
    if not res["lines"]:
        return

    shipment_id = order.number or f"purchase_order:{order.id}"
    existing = {
        row.sku_code: row
        for row in (
            await session.execute(
                select(LandedCost).where(LandedCost.purchase_order_id == order.id)
            )
        ).scalars().all()
    }
    unit_by_sku: dict[str, Decimal] = {}
    for r in res["lines"]:
        code, unit = r["sku_code"], r["unit_landed_cost"]
        unit_by_sku[code] = unit
        if code in existing:
            existing[code].unit_landed_cost_byn = unit
            existing[code].shipment_id = shipment_id
        else:
            session.add(
                LandedCost(
                    sku_code=code,
                    purchase_order_id=order.id,
                    shipment_id=shipment_id,
                    unit_landed_cost_byn=unit,
                    stage="estimated",
                )
            )
        # push-инвалидация себестоимости для sales + landed-маржа для finance (payload JSON-safe)
        event_bus.emit(
            session,
            "procurement.landed_cost.calculated",
            {
                "sku_code": code,
                "unit_landed_cost_byn": str(unit),
                "qty": str(agg[code]["qty"]),
                "total_landed_byn": str(r["landed_total"]),
                "shipment_id": shipment_id,
                "stage": "estimated",
                "purchase_order_id": order.id,
                "fx_rate": None,
                "fx_date": None,
                "fx_rate_basis": None,
                "entity_ref": f"purchase_order:{order.id}",
            },
        )

    # приход на склад ПО КАЖДОЙ позиции — оприходование по себестоимости (procurement → wms)
    for ln in lines:
        if not ln.sku_code or ln.qty <= 0:
            continue
        event_bus.emit(
            session,
            "procurement.received",
            {
                "sku_code": ln.sku_code,
                "qty": float(ln.qty),
                "warehouse": "Главный",  # ponytail: хардкод; апгрейд — поле warehouse на заказе
                "entity_ref": f"purchase_order:{order.id}:{ln.id}",
                "unit_landed_cost_byn": str(unit_by_sku.get(ln.sku_code, "")),
            },
        )


@router.get("/orders", response_model=list[PurchaseOrderOut])
async def list_orders(session: AsyncSession = Depends(get_session)):
    """Все заказы поставщикам с позициями (новые первыми)."""
    orders = (
        await session.execute(select(PurchaseOrder).order_by(PurchaseOrder.id.desc()))
    ).scalars().all()
    return await _orders_out(session, list(orders))


@router.get("/open-orders", response_model=list[PurchaseOrderOut])
async def open_orders(session: AsyncSession = Depends(get_session)):
    """Открытые заказы (товар не принят) с ETA — sales вычитает «в пути» по номенклатуре.
    Ближайший ETA первым (заказы без ETA — в конце)."""
    orders = (
        await session.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.status.in_(OPEN_ORDER_STATUSES))
            .order_by(PurchaseOrder.eta_date.asc().nulls_last(), PurchaseOrder.id.desc())
        )
    ).scalars().all()
    return await _orders_out(session, list(orders))


@router.get("/orders/{order_id}", response_model=PurchaseOrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_session)):
    """Один заказ с позициями (для экрана редактора машины)."""
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return (await _orders_out(session, [order]))[0]


@router.post("/orders", response_model=PurchaseOrderOut, status_code=201)
async def create_order(
    payload: PurchaseOrderCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Создать заказ поставщику с позициями. Номер генерируется, если не задан."""
    order = PurchaseOrder(
        supplier=payload.supplier,
        supplier_id=payload.supplier_id,
        number=payload.number,
        status=payload.status,
        eta_date=payload.eta_date,
        freight_byn=Decimal(str(payload.freight_byn)),
    )
    session.add(order)
    await session.flush()
    if not order.number:
        order.number = f"PO-2026-{order.id:04d}"
    for ln in payload.lines:
        session.add(_new_line(order.id, ln))
    if order.status == RECEIVED_ORDER_STATUS:  # создан сразу принятым — зафиксировать cost
        await session.flush()
        await _fixate_landed_cost(session, order, core.event_bus)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


def _validate_transition(current: str, new: str) -> None:
    """Машина состояний заказа: вперёд (со скипами) можно, назад — нельзя; отмена — из любого
    открытого (не принятого). Нелегальный переход → 422 (назад) / 409 (отмена принятого)."""
    if current == new:
        return  # идемпотентно
    if new == "cancelled":
        if current == RECEIVED_ORDER_STATUS:
            raise HTTPException(status_code=409, detail="Принятый заказ нельзя отменить")
        return
    cur, nxt = ORDER_RANK.get(current), ORDER_RANK.get(new)
    if cur is None or nxt is None:
        raise HTTPException(status_code=422, detail=f"Недопустимый переход статуса: {current}→{new}")
    if nxt < cur:
        raise HTTPException(status_code=422, detail=f"Нельзя откатить статус назад: {current}→{new}")


@router.patch("/orders/{order_id}", response_model=PurchaseOrderOut)
async def update_order_status(
    order_id: int,
    payload: PurchaseOrderStatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить статус заказа по машине состояний. Эмит ``procurement.order.status_changed`` на
    каждом переходе; при фактической приёмке (``received``) — фиксация landed cost + приход на склад."""
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    current = order.status
    _validate_transition(current, payload.status)
    if current != payload.status:
        order.status = payload.status
        core.event_bus.emit(
            session,
            "procurement.order.status_changed",
            {
                "order_id": order.id,
                "number": order.number,
                "from": current,
                "to": payload.status,
                "supplier_id": order.supplier_id,
            },
        )
        if payload.status == RECEIVED_ORDER_STATUS:
            await _fixate_landed_cost(session, order, core.event_bus)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


# ───────────────────── Редактор состава заказа (позиции + landed-preview) ─────────────────────


def _new_line(order_id: int, ln: PurchaseOrderLineIn) -> PurchaseOrderLine:
    return PurchaseOrderLine(
        order_id=order_id,
        sku_code=ln.sku_code,
        qty=Decimal(str(ln.qty)),
        goods_value_byn=Decimal(str(ln.goods_value_byn)),
        weight=Decimal(str(ln.weight)),
        volume=Decimal(str(ln.volume)),
    )


async def _require_editable_order(session: AsyncSession, order_id: int) -> PurchaseOrder:
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status == RECEIVED_ORDER_STATUS:
        raise HTTPException(status_code=409, detail="Принятый заказ нельзя редактировать")
    return order


@router.post("/orders/{order_id}/lines", response_model=PurchaseOrderOut, status_code=201)
async def add_order_line(
    order_id: int,
    payload: PurchaseOrderLineIn,
    session: AsyncSession = Depends(get_session),
):
    """Добавить позицию в заказ (редактор машины). Нельзя для принятого заказа (409)."""
    order = await _require_editable_order(session, order_id)
    session.add(_new_line(order.id, payload))
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


@router.delete("/orders/{order_id}/lines/{line_id}", response_model=PurchaseOrderOut)
async def delete_order_line(
    order_id: int,
    line_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Убрать позицию из заказа (редактор машины). Нельзя для принятого заказа (409)."""
    order = await _require_editable_order(session, order_id)
    line = await session.get(PurchaseOrderLine, line_id)
    if line is None or line.order_id != order.id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    await session.delete(line)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


@router.patch("/orders/{order_id}/header", response_model=PurchaseOrderOut)
async def update_order_header(
    order_id: int,
    payload: PurchaseOrderHeaderUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Править шапку заказа (фрахт/ETA/поставщик) в редакторе машины. Нельзя для принятого (409)."""
    order = await _require_editable_order(session, order_id)
    data = payload.model_dump(exclude_unset=True)
    if "freight_byn" in data:
        freight = data.pop("freight_byn")
        if freight is not None:  # freight не nullable — None игнорируем
            order.freight_byn = Decimal(str(freight))
    for field, value in data.items():
        setattr(order, field, value)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


@router.get("/orders/{order_id}/landed-preview")
async def landed_preview(order_id: int, session: AsyncSession = Depends(get_session)):
    """Предпросмотр распределения landed cost по позициям БЕЗ фиксации (live-пересчёт в редакторе).
    Тот же движок, что и на приёмке — фронт не считает сам."""
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    lines = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
        )
    ).scalars().all()
    res, _ = _order_allocation(lines, Decimal(order.freight_byn))
    return {
        "order_id": order.id,
        "freight_byn": float(order.freight_byn),
        "lines": [
            {
                "sku_code": r["sku_code"],
                "goods_byn": float(r["goods_value"]),
                "allocated_byn": float(r["allocated"]),
                "landed_total_byn": float(r["landed_total"]),
                "unit_landed_cost_byn": float(r["unit_landed_cost"]),
            }
            for r in res["lines"]
        ],
        "total_goods_byn": float(res["total_goods"]),
        "total_landed_byn": float(res["total_landed"]),
    }


# ───────────────────── Предв. себестоимость (Расчёт Китай) ─────────────────────


@router.post("/cost-estimate", response_model=CostEstimateOut)
async def cost_estimate(payload: CostEstimateRequest):
    """Предварительная (плановая) себестоимость импорта из Китая по позициям сделки/машины.

    Чистый расчёт без БД: цена поставщика + комиссия + страховка + фрахт + пошлина → landed
    cost BYN/шт (буфер курса ≥10%). Граница с продажами — ``unit_landed_cost_byn``; цена/наценка
    /НДС тут НЕ считаются (полоса «Маржа/ценообразование»). Ставка пошлины — на вход (авто-резолв
    из ``ref_tnved`` — Горизонт 2)."""
    r = payload.rates
    rates = CostRates(
        usd_byn=Decimal(str(r.usd_byn)),
        cny_rub=Decimal(str(r.cny_rub)),
        rub_byn=Decimal(str(r.rub_byn)),
        usd_rub=Decimal(str(r.usd_rub)),
        commission_pct=Decimal(str(r.commission_pct)),
        insurance_pct=Decimal(str(r.insurance_pct)),
        freight_usd_per_kg=Decimal(str(r.freight_usd_per_kg)),
        default_duty_pct=Decimal(str(r.default_duty_pct)),
        fx_buffer_pct=Decimal(str(r.fx_buffer_pct)),
    )
    lines = [
        CostLine(
            sku_code=ln.sku_code,
            path=ln.path,
            price=Decimal(str(ln.price)),
            qty=Decimal(str(ln.qty)),
            weight=Decimal(str(ln.weight)),
            duty_pct=None if ln.duty_pct is None else Decimal(str(ln.duty_pct)),
            util=Decimal(str(ln.util)),
        )
        for ln in payload.lines
    ]
    return estimate_china_cost(lines, rates)


# ───────────────────────── Справочник поставщиков ─────────────────────────


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(session: AsyncSession = Depends(get_session)):
    """Справочник поставщиков (новые первыми)."""
    return (
        await session.execute(select(Supplier).order_by(Supplier.id.desc()))
    ).scalars().all()


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_supplier(payload: SupplierCreate, session: AsyncSession = Depends(get_session)):
    """Завести поставщика. ``unp`` — soft-ref на MDM-контрагента (провенанс)."""
    obj = Supplier(**payload.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.get("/suppliers/{supplier_id}", response_model=SupplierOut)
async def get_supplier(supplier_id: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Supplier, supplier_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return obj


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
async def update_supplier(
    supplier_id: int, payload: SupplierUpdate, session: AsyncSession = Depends(get_session)
):
    obj = await session.get(Supplier, supplier_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.get("/suppliers/{supplier_id}/scorecard")
async def supplier_scorecard(supplier_id: int, session: AsyncSession = Depends(get_session)):
    """Скоркарта поставщика: заказы, претензии (откр/закр), средняя выигранная цена RFQ,
    компоненты и итоговый балл 0–10. Своевременность (ETA vs факт) — honest-empty: дат факта
    приёмки заказа пока нет."""
    obj = await session.get(Supplier, supplier_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    orders_count = int(
        (await session.execute(
            select(func.count()).where(PurchaseOrder.supplier_id == supplier_id)
        )).scalar_one()
    )
    claims = (
        await session.execute(
            select(SupplierClaim.status, func.count())
            .where(SupplierClaim.supplier_id == supplier_id)
            .group_by(SupplierClaim.status)
        )
    ).all()
    claims_by_status = {s: int(c) for s, c in claims}
    claims_total = sum(claims_by_status.values())
    claims_open = claims_total - sum(claims_by_status.get(s, 0) for s in CLAIM_CLOSED_STATUSES)
    avg_price = (
        await session.execute(
            select(func.avg(RfqBid.price_byn)).where(
                RfqBid.supplier_id == supplier_id, RfqBid.is_winner.is_(True)
            )
        )
    ).scalar_one_or_none()
    scoring = _score_components(orders_count, claims_total, on_time_rate=None)
    return {
        "supplier_id": supplier_id,
        "orders_count": orders_count,
        "claims_open": claims_open,
        "claims_closed": claims_total - claims_open,
        "claims_total": claims_total,
        "on_time_rate": None,  # honest-empty: нет дат факта приёмки заказа (ponytail: добавить received_at)
        "avg_won_price_byn": float(avg_price) if avg_price is not None else None,
        "components": scoring["components"],
        "score": scoring["score"],
    }


# ───────────────────────── RFQ / тендер закупки ─────────────────────────


def _rfq_out(rfq: Rfq, bids: list[RfqBid]) -> RfqOut:
    bids_sorted = sorted(bids, key=lambda b: b.price_byn)  # минимальная цена первой
    best_bid_id = bids_sorted[0].id if bids_sorted else None
    return RfqOut(
        id=rfq.id,
        item=rfq.item,
        sku_code=rfq.sku_code,
        qty=float(rfq.qty),
        request_id=rfq.request_id,
        status=rfq.status,
        due_date=rfq.due_date,
        bids=[RfqBidOut.model_validate(b) for b in bids_sorted],
        best_bid_id=best_bid_id,
    )


async def _bids_of(session: AsyncSession, rfq_id: int) -> list[RfqBid]:
    return list(
        (await session.execute(select(RfqBid).where(RfqBid.rfq_id == rfq_id))).scalars().all()
    )


@router.get("/rfq", response_model=list[RfqOut])
async def list_rfq(session: AsyncSession = Depends(get_session)):
    """Запросы цен (новые первыми) с предложениями и пометкой лучшей цены."""
    rfqs = (await session.execute(select(Rfq).order_by(Rfq.id.desc()))).scalars().all()
    ids = [r.id for r in rfqs]
    bids_by_rfq: dict[int, list[RfqBid]] = {}
    if ids:
        for b in (await session.execute(select(RfqBid).where(RfqBid.rfq_id.in_(ids)))).scalars().all():
            bids_by_rfq.setdefault(b.rfq_id, []).append(b)
    return [_rfq_out(r, bids_by_rfq.get(r.id, [])) for r in rfqs]


@router.post("/rfq", response_model=RfqOut, status_code=201)
async def create_rfq(payload: RfqCreate, session: AsyncSession = Depends(get_session)):
    """Создать запрос цен (тендер)."""
    rfq = Rfq(
        item=payload.item,
        sku_code=payload.sku_code,
        qty=Decimal(str(payload.qty)),
        request_id=payload.request_id,
        due_date=payload.due_date,
    )
    session.add(rfq)
    await session.commit()
    await session.refresh(rfq)
    return _rfq_out(rfq, [])


@router.get("/rfq/{rfq_id}", response_model=RfqOut)
async def get_rfq(rfq_id: int, session: AsyncSession = Depends(get_session)):
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="Запрос цен не найден")
    return _rfq_out(rfq, await _bids_of(session, rfq_id))


@router.post("/rfq/{rfq_id}/bids", response_model=RfqOut, status_code=201)
async def add_rfq_bid(
    rfq_id: int, payload: RfqBidIn, session: AsyncSession = Depends(get_session)
):
    """Добавить предложение поставщика к запросу цен."""
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="Запрос цен не найден")
    if rfq.status != "open":
        raise HTTPException(status_code=409, detail="Запрос цен закрыт — предложения не принимаются")
    session.add(
        RfqBid(
            rfq_id=rfq_id,
            supplier_id=payload.supplier_id,
            price_byn=Decimal(str(payload.price_byn)),
            lead_time_days=payload.lead_time_days,
            incoterms=payload.incoterms,
            note=payload.note,
        )
    )
    await session.commit()
    await session.refresh(rfq)
    return _rfq_out(rfq, await _bids_of(session, rfq_id))


@router.post("/rfq/{rfq_id}/award", response_model=RfqOut)
async def award_rfq(
    rfq_id: int,
    payload: RfqAward,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Выбрать победителя тендера: пометить bid победителем, закрыть RFQ, эмит ``procurement.rfq.awarded``."""
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="Запрос цен не найден")
    bids = await _bids_of(session, rfq_id)
    winner = next((b for b in bids if b.id == payload.bid_id), None)
    if winner is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено в этом запросе")
    for b in bids:
        b.is_winner = b.id == winner.id
    rfq.status = "awarded"
    core.event_bus.emit(
        session,
        "procurement.rfq.awarded",
        {
            "rfq_id": rfq.id,
            "supplier_id": winner.supplier_id,
            "price_byn": str(winner.price_byn),
            "sku_code": rfq.sku_code,
            "entity_ref": f"rfq:{rfq.id}",
        },
    )
    await session.commit()
    await session.refresh(rfq)
    return _rfq_out(rfq, await _bids_of(session, rfq_id))


# ───────────────────────── Претензии поставщикам ─────────────────────────


@router.get("/claims", response_model=list[SupplierClaimOut])
async def list_claims(session: AsyncSession = Depends(get_session)):
    """Претензии поставщикам (брак из производства и пр.), новые первыми."""
    return (
        await session.execute(select(SupplierClaim).order_by(SupplierClaim.id.desc()))
    ).scalars().all()


@router.post("/claims", response_model=SupplierClaimOut, status_code=201)
async def create_claim(payload: SupplierClaimCreate, session: AsyncSession = Depends(get_session)):
    """Ручное заведение претензии закупщиком (источник ``manual``)."""
    data = payload.model_dump()
    data["amount_byn"] = None if data["amount_byn"] is None else Decimal(str(data["amount_byn"]))
    obj = SupplierClaim(**data, status="open", source="manual")
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.patch("/claims/{claim_id}", response_model=SupplierClaimOut)
async def update_claim(
    claim_id: int,
    payload: SupplierClaimUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Назначить поставщика / урегулировать претензию. При resolved/rejected — эмит
    ``procurement.claim.resolved`` (finance/качество подпишутся)."""
    obj = await session.get(SupplierClaim, claim_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Претензия не найдена")
    was_closed = obj.status in CLAIM_CLOSED_STATUSES
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    if not was_closed and obj.status in CLAIM_CLOSED_STATUSES:
        core.event_bus.emit(
            session,
            "procurement.claim.resolved",
            {
                "claim_id": obj.id,
                "supplier_id": obj.supplier_id,
                "claim_type": obj.claim_type,
                "amount_byn": None if obj.amount_byn is None else str(obj.amount_byn),
                "resolution": obj.resolution,
                "status": obj.status,
                "entity_ref": f"claim:{obj.id}",
            },
        )
    await session.commit()
    await session.refresh(obj)
    return obj
