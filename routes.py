"""HTTP-API модуля Procurement. Монтируется под префиксом ``/procurement``."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard
from core.services import reference_query
from core.services.landed_cost import LandedExpense, LandedLine, allocate_landed_cost
from modules.procurement.cost_estimate import CostLine, CostRates, estimate_china_cost
from modules.procurement.models import (
    OPEN_ORDER_STATUSES,
    ORDER_RANK,
    RECEIVED_ORDER_STATUS,
    LandedCost,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderMilestone,
    PurchaseRequest,
    Rfq,
    RfqBid,
    ShipRequirement,
    Supplier,
    SupplierClaim,
    TransportMethod,
)
from modules.procurement.ownership import require_order_access, require_request_access
from modules.procurement.plan import (
    DEFAULT_METHODS,
    STAGE_ORDER,
    STAGE_TITLES,
    arrival_deadline,
    build_milestone_plan,
    total_transit_days,
)
from modules.procurement.schemas import (
    AtRiskDeal,
    CostEstimateOut,
    CostEstimateRequest,
    DeficitRequestIn,
    EditorHeaderInput,
    EditorLineInput,
    MilestoneOut,
    OrderMutationOut,
    OrderPlanIn,
    OrderPlanOut,
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
    ScopedOrderPlanOut,
    StageUpdate,
    SupplierClaimCreate,
    SupplierClaimOut,
    SupplierClaimUpdate,
    SupplierCreate,
    SupplierOut,
    SupplierUpdate,
    TransportMethodOut,
    TransportMethodUpdate,
)

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


async def _on_time_rates(session: AsyncSession, supplier_ids: set[int]) -> dict[int, float | None]:
    """Своевременность поставщика (батч): доля принятых заказов, доставленных не позже ETA
    (``received_at.date() <= eta_date``). В знаменателе — только заказы с обоими полями; без ETA
    или без факта приёмки не учитываются. Поставщик без квалифицирующих заказов → нет ключа
    (caller .get(sid) → None = honest-empty)."""
    if not supplier_ids:
        return {}
    rows = (
        await session.execute(
            select(PurchaseOrder.supplier_id, PurchaseOrder.received_at, PurchaseOrder.eta_date)
            .where(
                PurchaseOrder.supplier_id.in_(supplier_ids),
                PurchaseOrder.status == RECEIVED_ORDER_STATUS,
                PurchaseOrder.received_at.is_not(None),
                PurchaseOrder.eta_date.is_not(None),
            )
        )
    ).all()
    agg: dict[int, list[int]] = {}  # sid -> [on_time, total]
    for sid, received_at, eta_date in rows:
        bucket = agg.setdefault(sid, [0, 0])
        bucket[0] += 1 if received_at.date() <= eta_date else 0
        bucket[1] += 1
    return {sid: (ot / tot if tot else None) for sid, (ot, tot) in agg.items()}


async def _board_scores(session: AsyncSession, supplier_ids: set[int]) -> dict[int, float | None]:
    """Балл поставщика для карточек воронки (батч, без N+1): по заказам, претензиям, своевременности."""
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
            .where(
                SupplierClaim.supplier_id.in_(supplier_ids),
                SupplierClaim.status != "rejected",  # отклонённая претензия = поставщик не виноват
            )
            .group_by(SupplierClaim.supplier_id)
        )).all()
    )
    on_time = await _on_time_rates(session, supplier_ids)
    return {
        sid: _score_components(int(orders.get(sid, 0)), int(claims.get(sid, 0)), on_time.get(sid))["score"]
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
        # бейдж авто-источника: заявка, рождённая сигналом дефицита склада (P8). "" → нет бейджа.
        status_tag="Авто: дефицит склада" if r.origin == "deficit" else "",
        tags=[f"{r.qty} шт"] if r.qty else [],
    )


# ───────────────────────── Воронка закупок (PurchaseRequest) ─────────────────────────


@router.get("/requests", response_model=list[PurchaseRequestOut])
async def list_requests(session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


@router.get("/board", response_model=FunnelBoardOut)
async def board(session: AsyncSession = Depends(get_session)) -> FunnelBoardOut:
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


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


@router.patch("/requests/{req_id}", response_model=PurchaseRequestOut, dependencies=[Depends(require_request_access)])
async def update_request(
    req_id: int,
    payload: StageUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(require_request_access),
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
                "organization_id": organization_id,
                "qty": obj.qty,
                "warehouse": "Главный",
                "entity_ref": f"purchase:{obj.id}",
            },
        )
    await session.commit()
    await session.refresh(obj)
    return obj


async def _request_from_deficit(
    session: AsyncSession,
    *,
    sku_code: str,
    sku_title: str = "",
    warehouse: str = "Главный",
    deficit: float | Decimal = 0,
    reorder_qty: float | Decimal = 0,
    supplier_id: int | None = None,
) -> PurchaseRequest:
    """Дефицит склада → черновик заявки на закупку (MRP-lite). Переиспользуется подпиской на
    ``wms.stock.low`` и тонким эндпоинтом ``/requests/from-deficit``.

    Идемпотентно: повторный сигнал по той же позиции (origin='deficit', та же ``item``) среди
    незавершённых заявок не плодит дубль — возвращает существующую. Кол-во = ceil(reorder_qty
    или deficit), минимум 1. ``item`` = sku_title (fallback sku_code) — единый ключ дедупа.
    # ponytail: дедуп по item (у заявки нет колонки sku_code — миграция-фриз круга); для
    # авто-сигнала item стабилен (=sku_title), повтор того же события совпадает точно.
    """
    item_val = sku_title or sku_code
    existing = (
        await session.execute(
            select(PurchaseRequest).where(
                PurchaseRequest.origin == "deficit",
                PurchaseRequest.item == item_val,
                PurchaseRequest.stage != "done",
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing  # идемпотентность — повтор сигнала не плодит заявку
    amount = reorder_qty or deficit or 0
    qty = max(1, math.ceil(Decimal(str(amount))))
    obj = PurchaseRequest(
        supplier="",
        supplier_id=supplier_id,
        item=item_val,
        qty=qty,
        stage="need",
        origin="deficit",
        insight=f"Автозаявка по дефициту: {warehouse}, не хватает {deficit}",
    )
    session.add(obj)
    await session.flush()
    if not obj.number:
        obj.number = f"ЗАК-2026-{obj.id:04d}"
    return obj


@router.post("/requests/from-deficit", response_model=PurchaseRequestOut, status_code=201)
async def request_from_deficit(
    payload: DeficitRequestIn, session: AsyncSession = Depends(get_session)
):
    """Создать автозаявку из сигнала дефицита склада (debug/manual вход; штатный путь —
    подписка на ``wms.stock.low``). Идемпотентно по (origin='deficit', позиция)."""
    obj = await _request_from_deficit(
        session,
        sku_code=payload.sku_code,
        sku_title=payload.sku_title,
        warehouse=payload.warehouse,
        deficit=payload.deficit,
        reorder_qty=payload.reorder_qty,
        supplier_id=payload.supplier_id,
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
            received_at=o.received_at,
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
        # фрахт по весу — ТОЛЬКО если вес задан у ВСЕХ позиций; иначе по стоимости. Иначе позиция
        # с незаполненным весом получила бы 0 фрахта, а весь фрахт лёг бы на позиции с весом
        # (перекос per-SKU себестоимости → искажение маржи sales и оценки склада).
        all_weighted = all(ln.weight > 0 for ln in landed_lines)
        expenses.append(LandedExpense("фрахт", freight, "weight" if all_weighted else "value"))
    return allocate_landed_cost(landed_lines, expenses), agg


async def _recompute_estimated_landed(session: AsyncSession, sku_code: str) -> Decimal | None:
    """Пересчитать ПЛАНОВУЮ (``estimated``) себестоимость SKU по открытым (в пути) заказам и
    upsert одной строки ``LandedCost``. Вызывается при смене справочной ставки/мастер-поля
    (``reference.*.changed``, круг 4/B2) — даёт продажам живую дооприходную себестоимость,
    обновляемую при смене пошлины/мастер-данных. Возврат: новая себест/шт BYN или ``None``
    (нет открытых заказов с этим SKU — базы для оценки нет, не выдумываем нулём).

    Считаем ТОЛЬКО готовыми движками (контракт-фриз, методику не плодим): фрахт — общий
    ``allocate_landed_cost`` (внутри ``_order_allocation``), пошлину % — из фасада ядра
    ``sku_master.landed_inputs`` (REF3-1). Факт (``stage="actual"``) на приёмке имеет приоритет
    в фасаде себестоимости — плановая оценка его НЕ затирает. Коммит — у вызывающего (relay).
    """
    from core.services import sku_master  # фасад ядра; локальный импорт — без цикла модулей

    orders = (
        await session.execute(
            select(PurchaseOrder).where(PurchaseOrder.status.in_(OPEN_ORDER_STATUSES))
        )
    ).scalars().all()
    # средневзвешенная (по qty) customs-стоимость/шт (goods+фрахт) по всем открытым заказам с SKU
    # ponytail: O(заказы×SKU) при каскаде — перезагружаем заказы на каждый SKU; батчить, если вырастет
    total_qty, weighted = Decimal("0"), Decimal("0")
    for o in orders:
        lines = (
            await session.execute(
                select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == o.id)
            )
        ).scalars().all()
        res, agg = _order_allocation(lines, Decimal(o.freight_byn))
        for r in res["lines"]:
            if r["sku_code"] != sku_code:
                continue
            qty = agg[sku_code]["qty"]
            weighted += r["unit_landed_cost"] * qty
            total_qty += qty
    if total_qty <= 0:
        return None
    customs_unit = weighted / total_qty

    inputs = await sku_master.landed_inputs(session, sku_code)
    duty = inputs.get("duty_pct") if inputs else None  # % на дату или None (нет тарифа → без пошлины)
    duty_rate = Decimal(str(duty)) / Decimal("100") if duty is not None else Decimal("0")
    unit = (customs_unit * (Decimal("1") + duty_rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # одна плановая строка на SKU: purchase_order_id IS NULL (UNIQUE не дедупит NULL → upsert вручную)
    row = (
        await session.execute(
            select(LandedCost).where(
                LandedCost.sku_code == sku_code,
                LandedCost.purchase_order_id.is_(None),
                LandedCost.stage == "estimated",
            )
        )
    ).scalars().first()
    if row is not None:
        row.unit_landed_cost_byn = unit
    else:
        session.add(
            LandedCost(
                sku_code=sku_code,
                purchase_order_id=None,
                shipment_id="ref-estimate",
                unit_landed_cost_byn=unit,
                stage="estimated",
            )
        )
    return unit


async def _fixate_landed_cost(session: AsyncSession, order: PurchaseOrder, event_bus, organization_id: int) -> None:
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

    from core.services import sku_master  # фасад ядра; локальный импорт — без цикла модулей

    shipment_id = order.number or f"purchase_order:{order.id}"
    existing = {
        row.sku_code: row
        for row in (
            await session.execute(
                select(LandedCost).where(LandedCost.purchase_order_id == order.id)
            )
        ).scalars().all()
    }
    # Пошлина ТН ВЭД per-SKU на ДАТУ приёмки — тем же путём, что в плановой оценке
    # (``_recompute_estimated_landed``): % из фасада ядра ``sku_master.landed_inputs``, применённый
    # к таможенной стоимости (goods + разнесённый фрахт) ПОСЛЕ разнесения фрахта. Батчем (один
    # запрос на все SKU, не N+1). Нет кода/версии тарифа на дату → ``None`` → без пошлины (не нулём
    # маскируем, а просто 0% надбавки). Так план (estimated) и факт (actual) сходятся по пошлине.
    on_date = order.received_at.date() if order.received_at else None
    duty_inputs = await sku_master.landed_inputs_batch(
        session, [r["sku_code"] for r in res["lines"]], on_date
    )
    unit_by_sku: dict[str, Decimal] = {}
    for r in res["lines"]:
        code = r["sku_code"]
        inp = duty_inputs.get(code)
        duty = inp.get("duty_pct") if inp else None  # % на дату или None (нет тарифа → без пошлины)
        duty_rate = Decimal(str(duty)) / Decimal("100") if duty is not None else Decimal("0")
        unit = (r["unit_landed_cost"] * (Decimal("1") + duty_rate)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        # тот же множитель на таможенный итог — чтобы finance (читает total) видел landed С пошлиной
        landed_total = (r["landed_total"] * (Decimal("1") + duty_rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        unit_by_sku[code] = unit
        if code in existing:
            existing[code].unit_landed_cost_byn = unit
            existing[code].shipment_id = shipment_id
            existing[code].stage = "actual"  # повторная приёмка — снова факт
        else:
            session.add(
                LandedCost(
                    sku_code=code,
                    purchase_order_id=order.id,
                    shipment_id=shipment_id,
                    unit_landed_cost_byn=unit,
                    stage="actual",  # реальная приёмка (в отличие от плановой cost-estimate)
                )
            )
        # push-инвалидация себестоимости для sales + landed-маржа для finance (payload JSON-safe)
        event_bus.emit(
            session,
            "procurement.landed_cost.calculated",
            {
                "sku_code": code,
                "organization_id": organization_id,
                "unit_landed_cost_byn": str(unit),
                "qty": str(agg[code]["qty"]),
                "total_landed_byn": str(landed_total),
                "shipment_id": shipment_id,
                "stage": "actual",  # факт приёмки (finance читает qty+total, не ветвится по stage)
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
                "organization_id": organization_id,
                "qty": str(ln.qty),
                "warehouse": "Главный",  # ponytail: хардкод; апгрейд — поле warehouse на заказе
                "entity_ref": f"purchase_order:{order.id}:{ln.id}",
                "unit_landed_cost_byn": str(unit_by_sku.get(ln.sku_code, "")),
            },
        )


@router.get("/orders", response_model=list[PurchaseOrderOut])
async def list_orders(session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


@router.get("/open-orders", response_model=list[PurchaseOrderOut])
async def open_orders(session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


@router.get("/orders/{order_id}", response_model=PurchaseOrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


@router.post("/orders")
async def create_order():
    """Unscoped creation cannot establish a legal-entity owner or MDM supplier."""
    raise HTTPException(410, "Select an organization and use its catalog-bound order command")


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


def mutation_ack(order, org_id, principal, action, line_id=None):
    return {"organization_id": org_id, "principal": principal, "order_id": order.id,
            "action": action, "affected_line_id": line_id, "status": order.status,
            "received_at": order.received_at}


@router.patch("/orders/{order_id}", response_model=OrderMutationOut, dependencies=[Depends(require_order_access)])
async def update_order_status(
    order_id: int,
    payload: PurchaseOrderStatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
    organization_id: int = Depends(require_order_access),
):
    raise HTTPException(410, "Use durable scoped edit commands")


async def apply_order_status(order_id, payload, core, session, organization_id, x_expected_principal):
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
                "organization_id": organization_id,
                "number": order.number,
                "from": current,
                "to": payload.status,
                "supplier_id": order.supplier_id,
            },
        )
        if payload.status == RECEIVED_ORDER_STATUS:
            # наивный UTC (как sales._utcnow): один источник времени для received_at и факта ⑦
            received = datetime.now(timezone.utc).replace(tzinfo=None)
            order.received_at = received  # факт приёмки — основа своевременности (scorecard)
            await _fixate_landed_cost(session, order, core.event_bus, organization_id)
            await _mark_arrival_fact(session, order.id, received.date())  # факт ⑦ В Минске в план машины
    result = mutation_ack(order, organization_id, x_expected_principal, "status")
    return result


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
    if order.status in (RECEIVED_ORDER_STATUS, "cancelled"):  # терминальные — состав/шапку не меняем
        raise HTTPException(status_code=409, detail="Принятый/отменённый заказ нельзя редактировать")
    return order


@router.post("/orders/{order_id}/lines", response_model=OrderMutationOut, status_code=201, dependencies=[Depends(require_order_access)])
async def add_order_line(
    order_id: int,
    payload: EditorLineInput,
    organization_id: int = Depends(require_order_access),
    session: AsyncSession = Depends(get_session),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
):
    raise HTTPException(410, "Use durable scoped edit commands")


async def apply_add_line(order_id, payload, session, organization_id, x_expected_principal):
    """Добавить позицию в заказ (редактор машины). Нельзя для принятого заказа (409)."""
    order = await _require_editable_order(session, order_id)
    line = _new_line(order.id, payload)
    session.add(line)
    await session.flush()
    result = mutation_ack(order, organization_id, x_expected_principal, "add_line", line.id)
    return result


@router.delete("/orders/{order_id}/lines/{line_id}", response_model=OrderMutationOut, dependencies=[Depends(require_order_access)])
async def delete_order_line(
    order_id: int,
    line_id: int,
    organization_id: int = Depends(require_order_access),
    session: AsyncSession = Depends(get_session),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
):
    raise HTTPException(410, "Use durable scoped edit commands")


async def apply_delete_line(order_id, line_id, session, organization_id, x_expected_principal):
    """Убрать позицию из заказа (редактор машины). Нельзя для принятого заказа (409)."""
    order = await _require_editable_order(session, order_id)
    line = await session.get(PurchaseOrderLine, line_id)
    if line is None or line.order_id != order.id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    await session.delete(line)
    result = mutation_ack(order, organization_id, x_expected_principal, "delete_line", line_id)
    return result


@router.patch("/orders/{order_id}/header", response_model=OrderMutationOut, dependencies=[Depends(require_order_access)])
async def update_order_header(
    order_id: int,
    payload: EditorHeaderInput,
    organization_id: int = Depends(require_order_access),
    session: AsyncSession = Depends(get_session),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
):
    raise HTTPException(410, "Use durable scoped edit commands")


async def apply_order_header(order_id, payload, session, organization_id, x_expected_principal):
    """Править шапку заказа (фрахт/ETA/поставщик) в редакторе машины. Нельзя для принятого (409)."""
    order = await _require_editable_order(session, order_id)
    data = payload.model_dump(exclude_unset=True)
    if "freight_byn" in data:
        freight = data.pop("freight_byn")
        if freight is not None:  # freight не nullable — None игнорируем
            order.freight_byn = Decimal(str(freight))
    for field, value in data.items():
        setattr(order, field, value)
    result = mutation_ack(order, organization_id, x_expected_principal, "header")
    return result


@router.get("/orders/{order_id}/landed-preview")
async def landed_preview(order_id: int):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


async def order_landed_preview(session: AsyncSession, order: PurchaseOrder):
    lines = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
        )
    ).scalars().all()
    res, _ = _order_allocation(lines, Decimal(order.freight_byn))

    from core.services import sku_master  # фасад ядра; локальный импорт — без цикла модулей

    duty_inputs = await sku_master.landed_inputs_batch(session, [r["sku_code"] for r in res["lines"]])
    adjusted_lines = []
    total_landed = Decimal("0")
    for r in res["lines"]:
        inp = duty_inputs.get(r["sku_code"])
        duty = inp.get("duty_pct") if inp else None
        duty_rate = Decimal(str(duty)) / Decimal("100") if duty is not None else Decimal("0")
        unit = (r["unit_landed_cost"] * (Decimal("1") + duty_rate)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        landed_total = (r["landed_total"] * (Decimal("1") + duty_rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total_landed += landed_total
        adjusted_lines.append(
            {
                "sku_code": r["sku_code"],
                "goods_byn": str(r["goods_value"]),
                "allocated_byn": str(r["allocated"]),
                "landed_total_byn": str(landed_total),
                "unit_landed_cost_byn": str(unit),
            }
        )
    return {
        "order_id": order.id,
        "freight_byn": str(order.freight_byn),
        "lines": adjusted_lines,
        "total_goods_byn": str(res["total_goods"]),
        "total_landed_byn": str(total_landed),
    }


# ───────────────────── Предв. себестоимость (Расчёт Китай) ─────────────────────


@router.post("/cost-estimate", response_model=CostEstimateOut)
async def cost_estimate(payload: CostEstimateRequest, session: AsyncSession = Depends(get_session)):
    """Предварительная (плановая) себестоимость импорта из Китая по позициям сделки/машины.

    Чистый расчёт без БД: цена поставщика + комиссия + страховка + фрахт + пошлина → landed
    cost BYN/шт (буфер курса ≥10%). Граница с продажами — ``unit_landed_cost_byn``; цена/наценка
    /НДС тут НЕ считаются (полоса «Маржа/ценообразование»). Ставка пошлины — на вход (авто-резолв
    из ``ref_tnved`` — Горизонт 2)."""
    r = payload.rates
    from core.services import nbrb

    on = payload.operation_date or nbrb.today()
    try:
        quotes = {code: await nbrb.quote(session, code, on) for code in ("USD", "RUB", "CNY")}
    except nbrb.RateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    usd, rub, cny = (Decimal(quotes[code]["rate"]) for code in ("USD", "RUB", "CNY"))
    rates = CostRates(
        usd_byn=usd,
        cny_rub=cny / rub,
        rub_byn=rub,
        usd_rub=usd / rub,
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
    result = estimate_china_cost(lines, rates)
    result["fx_quotes"] = quotes
    await session.commit()
    return result


# ───────────────────────── Справочник поставщиков ─────────────────────────


@router.get("/supplier-counterparties")
async def supplier_counterparties(q: str = Query(min_length=2, max_length=100),
                                  session: AsyncSession = Depends(get_session)):
    """Bounded active-MDM picker for procurement users, not a list-all export."""
    term = q.strip()
    if len(term) < 2:
        raise HTTPException(422, "Введите минимум два символа")
    result = await reference_query.query(session, "core.counterparties", name=term, limit=20)
    ids = [row["id"] for row in result["result"]]
    parties = {party.id: party for party in (await session.scalars(
        select(Counterparty).where(Counterparty.id.in_(ids)))).all()} if ids else {}
    return {"items": [{"id": identity, "name": parties[identity].legal_name or parties[identity].name,
                       "unp": parties[identity].unp or ""} for identity in ids if identity in parties]}


async def _verify_supplier_identity(session: AsyncSession, counterparty_id: int,
                                    name: str, unp: str, supplier_id: int | None = None) -> None:
    """Bind only an active MDM record; never infer identity from a matching UNP."""
    party = await session.scalar(select(Counterparty).where(
        Counterparty.id == counterparty_id).with_for_update(read=True))
    if (party is None or not party.is_active or party.merged_into_id is not None
            or name != (party.legal_name or party.name) or unp != (party.unp or "")):
        raise HTTPException(409, "Контрагент MDM изменился или недоступен; выберите его заново")
    existing = await session.scalar(select(Supplier.id).where(Supplier.counterparty_id == counterparty_id))
    if existing is not None and existing != supplier_id:
        raise HTTPException(409, "Для этого контрагента уже есть профиль поставщика")


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(session: AsyncSession = Depends(get_session)):
    """Справочник поставщиков (новые первыми)."""
    return (
        await session.execute(select(Supplier).order_by(Supplier.id.desc()))
    ).scalars().all()


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_supplier(payload: SupplierCreate, session: AsyncSession = Depends(get_session)):
    """New bound profiles carry a checked MDM ID; legacy unbound API remains readable."""
    if payload.counterparty_id is not None:
        await _verify_supplier_identity(session, payload.counterparty_id, payload.name, payload.unp)
    obj = Supplier(**payload.model_dump())
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Профиль контрагента уже существует") from exc
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
    changes = payload.model_dump(exclude_unset=True)
    target_id = changes.get("counterparty_id", obj.counterparty_id)
    if obj.counterparty_id is not None and target_id != obj.counterparty_id:
        raise HTTPException(409, "Нельзя заменить подтверждённый ID контрагента поставщика")
    if target_id is not None and {"counterparty_id", "name", "unp"}.intersection(changes):
        await _verify_supplier_identity(session, target_id, changes.get("name", obj.name),
                                        changes.get("unp", obj.unp), obj.id)
    for field, value in changes.items():
        setattr(obj, field, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Профиль контрагента уже существует") from exc
    await session.refresh(obj)
    return obj


@router.get("/suppliers/{supplier_id}/scorecard")
async def supplier_scorecard(supplier_id: int, session: AsyncSession = Depends(get_session)):
    """Скоркарта поставщика: заказы, претензии (откр/закр), средняя выигранная цена RFQ,
    компоненты и итоговый балл 0–10. Своевременность — доля заказов, принятых не позже ETA
    (received_at vs eta_date); None, если нет заказов с обоими полями (honest-empty)."""
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
    # качество: отклонённые претензии (поставщик не виноват) НЕ снижают балл
    claims_for_quality = claims_total - claims_by_status.get("rejected", 0)
    avg_price = (
        await session.execute(
            select(func.avg(RfqBid.price_byn)).where(
                RfqBid.supplier_id == supplier_id, RfqBid.is_winner.is_(True)
            )
        )
    ).scalar_one_or_none()
    # своевременность: ETA vs факт приёмки (received_at) по принятым заказам поставщика
    on_time_rate = (await _on_time_rates(session, {supplier_id})).get(supplier_id)
    scoring = _score_components(orders_count, claims_for_quality, on_time_rate)
    return {
        "supplier_id": supplier_id,
        "orders_count": orders_count,
        "claims_open": claims_open,
        "claims_closed": claims_total - claims_open,
        "claims_total": claims_total,
        "on_time_rate": on_time_rate,  # None — нет заказов с ETA+фактом (honest-empty)
        "avg_won_price_byn": float(avg_price) if avg_price is not None else None,
        "components": scoring["components"],
        "score": scoring["score"],
    }


# ───────────────────────── RFQ / тендер закупки ─────────────────────────


def _rfq_out(rfq: Rfq, bids: list[RfqBid], created_order_id: int | None = None) -> RfqOut:
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
        created_order_id=created_order_id,
    )


async def _bids_of(session: AsyncSession, rfq_id: int) -> list[RfqBid]:
    return list(
        (await session.execute(select(RfqBid).where(RfqBid.rfq_id == rfq_id))).scalars().all()
    )


@router.get("/rfq", response_model=list[RfqOut])
async def list_rfq(session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and an owned purchase request")


@router.post("/rfq", response_model=RfqOut, status_code=201)
async def create_rfq(payload: RfqCreate, session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and an owned purchase request")


@router.get("/rfq/{rfq_id}", response_model=RfqOut)
async def get_rfq(rfq_id: int, session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its RFQ route")


@router.post("/rfq/{rfq_id}/bids", response_model=RfqOut, status_code=201)
async def add_rfq_bid(
    rfq_id: int, payload: RfqBidIn, session: AsyncSession = Depends(get_session)
):
    raise HTTPException(410, "Select an organization and use its RFQ route")


@router.post("/rfq/{rfq_id}/award", response_model=RfqOut)
async def award_rfq(
    rfq_id: int,
    payload: RfqAward,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    raise HTTPException(410, "Select an organization and use its RFQ route")


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
        # P6: резолв номера заказа (order_code → PurchaseOrder.number) для finance. None, если
        # претензия не привязана к заказу. Себестоимость НЕ пересчитываем (риск порчи факт-cost).
        # ponytail: фактический пересчёт unit_landed_cost по возврату — Горизонт 2.
        order_id = None
        if obj.order_code:
            order_id = (
                await session.execute(
                    select(PurchaseOrder.id).where(PurchaseOrder.number == obj.order_code)
                )
            ).scalar_one_or_none()
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
                "order_id": order_id,
                "entity_ref": f"claim:{obj.id}",
            },
        )
    await session.commit()
    await session.refresh(obj)
    return obj


# ───────────────────────── План сбора машины (этапы Китай → Минск) ─────────────────────────


async def _ensure_default_methods(session: AsyncSession) -> None:
    """Засеять справочник способов перевозки дефолтами (Контейнер/Машина), если кода нет.
    Идемпотентно + устойчиво к гонке первого старта: вставку в savepoint, IntegrityError
    глотаем (параллельный запрос успел засеять те же коды — это ок). Работает и в SQLite-dev."""
    have = set((await session.execute(select(TransportMethod.code))).scalars().all())
    missing = [(c, s) for c, s in DEFAULT_METHODS.items() if c not in have]
    if not missing:
        return
    try:
        async with session.begin_nested():  # savepoint: конфликт уникальности не валит запрос
            for code, spec in missing:
                session.add(
                    TransportMethod(code=code, name=spec["name"], durations=dict(spec["durations"]))
                )
    except IntegrityError:
        pass  # параллельный первый старт уже засеял эти коды


def _method_out(m: TransportMethod) -> TransportMethodOut:
    durations = m.durations or {}
    return TransportMethodOut(
        code=m.code, name=m.name, durations=durations,
        total_days=total_transit_days(durations), active=m.active,
    )


@router.get("/transport-methods", response_model=list[TransportMethodOut])
async def list_transport_methods(session: AsyncSession = Depends(get_session)):
    """Справочник способов перевозки с длительностями этапов (Контейнер/Машина; редактируемый)."""
    await _ensure_default_methods(session)
    await session.commit()
    rows = (await session.execute(select(TransportMethod).order_by(TransportMethod.id))).scalars().all()
    return [_method_out(m) for m in rows]


@router.patch("/transport-methods/{code}", response_model=TransportMethodOut)
async def update_transport_method(
    code: str, payload: TransportMethodUpdate, session: AsyncSession = Depends(get_session)
):
    """Править способ перевозки: название / длительности этапов / активность (мерж длительностей)."""
    await _ensure_default_methods(session)
    m = (
        await session.execute(select(TransportMethod).where(TransportMethod.code == code))
    ).scalars().first()
    if m is None:
        raise HTTPException(status_code=404, detail="Способ перевозки не найден")
    data = payload.model_dump(exclude_unset=True)
    durations = data.pop("durations", None)
    if durations is not None:
        m.durations = {**(m.durations or {}), **{k: int(v) for k, v in durations.items()}}
    for field, value in data.items():
        if value is not None:
            setattr(m, field, value)
    await session.commit()
    await session.refresh(m)
    return _method_out(m)


async def _order_milestones(session: AsyncSession, order_id: int) -> list[PurchaseOrderMilestone]:
    return list(
        (await session.execute(
            select(PurchaseOrderMilestone)
            .where(PurchaseOrderMilestone.order_id == order_id)
            .order_by(PurchaseOrderMilestone.seq)
        )).scalars().all()
    )


async def _order_requirements(session: AsyncSession, order_id: int) -> list[ShipRequirement]:
    """Требования клиентов, релевантные машине — по sku_code её позиций (срок отгрузки из продаж).
    # ponytail: матч по sku (нет связи позиция→сделка); точная привязка — следующий слой."""
    skus = {
        s for s in (await session.execute(
            select(PurchaseOrderLine.sku_code).where(PurchaseOrderLine.order_id == order_id)
        )).scalars().all() if s
    }
    if not skus:
        return []
    return list((await session.execute(
        select(ShipRequirement).where(ShipRequirement.sku_code.in_(skus))
    )).scalars().all())


def _plan_out(
    order: PurchaseOrder,
    milestones: list[PurchaseOrderMilestone],
    requirements: list[ShipRequirement],
    today: date,
) -> OrderPlanOut:
    ms = [
        MilestoneOut(
            stage=m.stage, title=STAGE_TITLES.get(m.stage, m.stage), seq=m.seq,
            duration_days=m.duration_days, planned_date=m.planned_date, actual_date=m.actual_date,
        )
        for m in milestones
    ]
    # «Спланирован заказ» = окончание первого этапа − его длительность
    start = None
    if milestones and milestones[0].planned_date is not None:
        start = milestones[0].planned_date - timedelta(days=milestones[0].duration_days)
    target = order.target_arrival_date

    # Ограничение от срока клиента: машина должна прийти к (самый ранний срок − буфер). Иначе риск.
    dated = [r for r in requirements if r.ship_deadline_date is not None]
    required_by = min((r.ship_deadline_date for r in dated), default=None)
    required_arrival = arrival_deadline(required_by)
    slack_days = (required_arrival - target).days if (required_arrival and target) else None
    # есть срок клиента, но машина не запланирована (target=None) ИЛИ приходит позже крайней даты —
    # риск срыва. Незапланированная машина с живым сроком — наивысший риск, не «зелёная».
    at_risk = bool(required_arrival and (target is None or target > required_arrival))
    if start is not None and start < today:
        at_risk = True  # старт сбора уже в прошлом — не успеть запустить машину
    at_risk_deals: list[AtRiskDeal] = []
    for r in dated:
        r_arr = arrival_deadline(r.ship_deadline_date)
        r_slack = (r_arr - target).days if (target and r_arr) else None
        if r_slack is not None and r_slack < 0:  # опоздание → штрафной риск
            at_risk_deals.append(AtRiskDeal(
                deal_id=r.deal_id, number=r.number, counterparty=r.counterparty, sku_code=r.sku_code,
                ship_deadline=r.ship_deadline, required_arrival=r_arr, slack_days=r_slack,
                penalty_rate_pct=float(r.penalty_rate_pct) if r.penalty_rate_pct is not None else None,
                penalty_cap_pct=float(r.penalty_cap_pct) if r.penalty_cap_pct is not None else None,
                penalty_terms=r.penalty_terms,
            ))
    return OrderPlanOut(
        order_id=order.id,
        transport_method_code=order.transport_method_code,
        target_arrival_date=target,
        start_date=start,
        total_days=sum(m.duration_days for m in milestones),
        milestones=ms,
        required_by=required_by,
        required_arrival=required_arrival,
        slack_days=slack_days,
        at_risk=at_risk,
        at_risk_deals=at_risk_deals,
    )


def owned_plan_out(org_id, order, milestones, today, principal=None):
    start = None
    if milestones and milestones[0].planned_date is not None:
        start = milestones[0].planned_date - timedelta(days=milestones[0].duration_days)
    return ScopedOrderPlanOut(
        organization_id=org_id, principal=principal, order_id=order.id,
        transport_method_code=order.transport_method_code,
        target_arrival_date=order.target_arrival_date, start_date=start,
        total_days=sum(m.duration_days for m in milestones),
        milestones=[MilestoneOut(stage=m.stage, title=STAGE_TITLES.get(m.stage, m.stage),
            seq=m.seq, duration_days=m.duration_days, planned_date=m.planned_date,
            actual_date=m.actual_date) for m in milestones],
        schedule_start_in_past=start < today if start is not None else None,
    )


@router.get("/orders/{order_id}/plan", response_model=OrderPlanOut)
async def get_order_plan(order_id: int, session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Select an organization and use its procurement endpoints")


@router.post("/orders/{order_id}/plan", response_model=ScopedOrderPlanOut, dependencies=[Depends(require_order_access)])
async def plan_order(
    order_id: int, payload: OrderPlanIn, session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(require_order_access),
    x_expected_principal: str | None = Header(default=None, min_length=1, max_length=200),
):
    raise HTTPException(410, "Use durable scoped edit commands")


async def apply_order_plan(order_id, payload, session, organization_id, x_expected_principal):
    """Запланировать/пересчитать график сбора машины: способ перевозки + дедлайн «В Минске до».
    Этапы — обратный waterfall от target_arrival_date; факт (actual_date) этапов сохраняется."""
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    method = (
        await session.execute(
            select(TransportMethod).where(TransportMethod.code == payload.transport_method_code)
        )
    ).scalars().first()
    if method is None and payload.transport_method_code in DEFAULT_METHODS:
        spec = DEFAULT_METHODS[payload.transport_method_code]
        # Defaults are input data, not a separate write inside this command.
        method = TransportMethod(code=payload.transport_method_code, name=spec["name"],
                                 durations=dict(spec["durations"]))
    if method is None:
        raise HTTPException(status_code=404, detail="Способ перевозки не найден")
    target = payload.target_arrival_date
    durations = method.durations or DEFAULT_METHODS.get(method.code, {}).get("durations", {})
    plans, _start = build_milestone_plan(durations, target)

    order.transport_method_code = method.code
    order.target_arrival_date = target
    existing = {m.stage: m for m in await _order_milestones(session, order_id)}
    for p in plans:
        m = existing.get(p["stage"])
        if m is not None:  # пересчёт: план обновляем, факт (actual_date) НЕ трогаем
            m.seq, m.duration_days, m.planned_date = p["seq"], p["duration_days"], p["planned_date"]
        else:
            session.add(PurchaseOrderMilestone(
                order_id=order.id, stage=p["stage"], seq=p["seq"],
                duration_days=p["duration_days"], planned_date=p["planned_date"],
            ))
    await session.flush()
    result = owned_plan_out(organization_id, order, await _order_milestones(session, order_id),
                            datetime.now(timezone.utc).date(), x_expected_principal)
    return result



async def _mark_arrival_fact(session: AsyncSession, order_id: int, arrival_date) -> None:
    """Факт прихода в Минск (⑦): проставить actual_date последнего этапа (растоможка/В Минске)
    при приёмке заказа, если у машины есть план. ``arrival_date`` берём тем же часам, что и
    ``received_at`` заказа (наивный UTC) — чтобы план↔факт и своевременность не разъехались на день."""
    last = (
        await session.execute(
            select(PurchaseOrderMilestone).where(
                PurchaseOrderMilestone.order_id == order_id,
                PurchaseOrderMilestone.stage == STAGE_ORDER[-1],
            )
        )
    ).scalars().first()
    if last is not None and last.actual_date is None:
        last.actual_date = arrival_date
