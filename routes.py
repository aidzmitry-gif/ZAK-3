"""HTTP-API модуля Procurement. Монтируется под префиксом ``/procurement``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from core.services.landed_cost import LandedExpense, LandedLine, allocate_landed_cost
from modules.procurement.cost_estimate import CostLine, CostRates, estimate_china_cost
from modules.procurement.models import (
    OPEN_ORDER_STATUSES,
    RECEIVED_ORDER_STATUS,
    LandedCost,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    SupplierClaim,
)
from modules.procurement.schemas import (
    CostEstimateOut,
    CostEstimateRequest,
    PurchaseOrderCreate,
    PurchaseOrderLineOut,
    PurchaseOrderOut,
    PurchaseOrderStatusUpdate,
    PurchaseRequestCreate,
    PurchaseRequestOut,
    StageUpdate,
    SupplierClaimOut,
    SupplierClaimUpdate,
)
from modules.procurement.stages import STAGES

router = APIRouter(tags=["procurement"])

# Переход в эту стадию воронки = товар физически принят → приход на склад (procurement → wms).
RECEIVED_STAGE = "qc"


def _to_card(r: PurchaseRequest) -> FunnelCard:
    # Supplier Score — на стадиях переговоров/анализа (как в референсе)
    score = "Score 8.7" if r.stage in ("nego", "analysis") else ""
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
        score=score,
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
    """Воронка закупок: заявки сгруппированы по стадиям sourcing-цикла."""
    rows = (await session.execute(select(PurchaseRequest))).scalars().all()
    return build_board(STAGES, rows, _to_card)


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


# ───────────────────────── Открытые заказы (PurchaseOrder) + landed cost ─────────────────────────


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
            status=o.status,
            eta_date=o.eta_date,
            freight_byn=float(o.freight_byn),
            lines=[PurchaseOrderLineOut.model_validate(ln) for ln in lines_by_order.get(o.id, [])],
        )
        for o in orders
    ]


async def _fixate_landed_cost(session: AsyncSession, order: PurchaseOrder, event_bus) -> None:
    """На приёмке (``received``) разнести фрахт заказа на позиции и зафиксировать
    себестоимость единицы per-SKU через общий движок ``allocate_landed_cost`` (не второй
    расчёт). Upsert по (sku_code, заказ): повторная приёмка не плодит дубль. По каждой
    зафиксированной номенклатуре эмитит ``procurement.landed_cost.calculated`` (push-
    инвалидация снапшота себестоимости в sales → пересчёт маржи).

    Позиции с одинаковым ``sku_code`` агрегируем в одну (одна строка ``LandedCost`` на
    номенклатуру). Мин.срез: издержки = только фрахт; пошлина по ТН ВЭД, два FX-курса и
    буфер курса +10% — Горизонт 2 (методика «Расчёт Китай», docs/landed-cost.md).
    """
    lines = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
        )
    ).scalars().all()
    agg: dict[str, dict[str, Decimal]] = {}
    for ln in lines:
        if not ln.sku_code or ln.qty <= 0:
            continue  # без номенклатуры или с нулевым кол-вом: себестоимость единицы не определена
            # (иначе записали бы unit=0 → замаскировали бы дыру в марже, нарушив «None ≠ 0»)
        a = agg.setdefault(
            ln.sku_code,
            {"qty": Decimal("0"), "goods": Decimal("0"), "weight": Decimal("0"), "volume": Decimal("0")},
        )
        a["qty"] += Decimal(ln.qty)
        a["goods"] += Decimal(ln.goods_value_byn)
        a["weight"] += Decimal(ln.weight)
        a["volume"] += Decimal(ln.volume)
    if not agg:
        return

    landed_lines = [
        LandedLine(sku_code=code, qty=a["qty"], weight=a["weight"], volume=a["volume"], goods_value=a["goods"])
        for code, a in agg.items()
    ]
    expenses: list[LandedExpense] = []
    freight = Decimal(order.freight_byn)
    if freight:
        # фрахт по весу (как Odoo); если веса не заданы — по стоимости
        total_weight = sum((ln.weight for ln in landed_lines), Decimal("0"))
        expenses.append(LandedExpense("фрахт", freight, "weight" if total_weight > 0 else "value"))

    res = allocate_landed_cost(landed_lines, expenses)
    shipment_id = order.number or f"purchase_order:{order.id}"
    existing = {
        row.sku_code: row
        for row in (
            await session.execute(
                select(LandedCost).where(LandedCost.purchase_order_id == order.id)
            )
        ).scalars().all()
    }
    for r in res["lines"]:
        code, unit = r["sku_code"], r["unit_landed_cost"]
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
        # push-инвалидация для sales: себестоимость по номенклатуре пересчитана (§8).
        # payload — JSON-safe (Decimal→str); подписчиков пока нет (sales подпишется позже).
        event_bus.emit(
            session,
            "procurement.landed_cost.calculated",
            {
                "sku_code": code,
                "unit_landed_cost_byn": str(unit),
                # qty + итог по позиции — Финансам для landed-маржи (unit×qty или total)
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
    Ближайший ETA первым (заказы без ETA — в конце по сортировке БД)."""
    orders = (
        await session.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.status.in_(OPEN_ORDER_STATUSES))
            # nulls_last явно: в SQLite NULL по умолчанию идут первыми, в Postgres — последними
            .order_by(PurchaseOrder.eta_date.asc().nulls_last(), PurchaseOrder.id.desc())
        )
    ).scalars().all()
    return await _orders_out(session, list(orders))


@router.post("/orders", response_model=PurchaseOrderOut, status_code=201)
async def create_order(
    payload: PurchaseOrderCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Создать заказ поставщику с позициями. Номер генерируется, если не задан."""
    order = PurchaseOrder(
        supplier=payload.supplier,
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
        session.add(
            PurchaseOrderLine(
                order_id=order.id,
                sku_code=ln.sku_code,
                qty=Decimal(str(ln.qty)),
                goods_value_byn=Decimal(str(ln.goods_value_byn)),
                weight=Decimal(str(ln.weight)),
                volume=Decimal(str(ln.volume)),
            )
        )
    if order.status == RECEIVED_ORDER_STATUS:  # создан сразу принятым — зафиксировать cost
        await session.flush()
        await _fixate_landed_cost(session, order, core.event_bus)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


@router.patch("/orders/{order_id}", response_model=PurchaseOrderOut)
async def update_order_status(
    order_id: int,
    payload: PurchaseOrderStatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить статус заказа. При фактической приёмке (``received``) — зафиксировать
    landed cost по позициям. Повторная приёмка дубль не плодит (upsert)."""
    order = await session.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    entering_received = (
        order.status != RECEIVED_ORDER_STATUS and payload.status == RECEIVED_ORDER_STATUS
    )
    order.status = payload.status
    if entering_received:
        await _fixate_landed_cost(session, order, core.event_bus)
    await session.commit()
    await session.refresh(order)
    return (await _orders_out(session, [order]))[0]


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


# ───────────────────────── Претензии поставщикам ─────────────────────────


@router.get("/claims", response_model=list[SupplierClaimOut])
async def list_claims(session: AsyncSession = Depends(get_session)):
    """Претензии поставщикам (брак из производства и пр.), новые первыми."""
    return (
        await session.execute(select(SupplierClaim).order_by(SupplierClaim.id.desc()))
    ).scalars().all()


@router.patch("/claims/{claim_id}", response_model=SupplierClaimOut)
async def update_claim(
    claim_id: int,
    payload: SupplierClaimUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Назначить поставщика и/или сменить статус претензии (закупщик)."""
    obj = await session.get(SupplierClaim, claim_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Претензия не найдена")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await session.commit()
    await session.refresh(obj)
    return obj
