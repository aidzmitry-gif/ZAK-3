"""Обработчики событий модуля Procurement."""
from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger("aios.procurement")


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def on_production_scrap(payload: dict, ctx) -> None:
    """Брак в производстве (ОТК) → автопретензия поставщику (production → procurement).

    Замыкает цикл качества: дефект, найденный на сборке (``production.scrap``),
    фиксируется претензией в закупках. Поставщик в событии не приходит — открываем
    претензию без поставщика (``status="open"``); закупщик проставит поставщика и
    закроет её через ``PATCH /procurement/claims``. Обработчик с контекстом: пишет
    в сессию relay, коммит делает relay (не здесь) — §2.5.
    """
    if ctx is None:
        return
    from modules.procurement.models import SupplierClaim

    ctx.session.add(
        SupplierClaim(
            item=payload.get("item", ""),
            reason=payload.get("reason", ""),
            order_code=payload.get("order_code", ""),
            entity_ref=payload.get("entity_ref", ""),
            status="open",
            source="production",
        )
    )
    logger.info(
        "Procurement: претензия по браку — %s (наряд %s)",
        payload.get("item"),
        payload.get("order_code"),
    )


async def on_stock_low(payload: dict, ctx) -> None:
    """Сигнал дефицита склада → авто-черновик заявки на закупку (wms → procurement, MRP-lite).

    Канонический ПЛОСКИЙ payload (одно событие на нарушенный порог): ``{sku_code, sku_title,
    warehouse, free_qty, min_qty, deficit, reorder_qty, severity, source, entity_ref}``. Создание
    и идемпотентность держит ``_request_from_deficit`` (переиспользуем, не дублируем). Обработчик
    с ``(payload, ctx)``: пишет в сессию relay, коммит делает relay (не здесь) — §2.5.
    """
    if ctx is None:
        return
    sku_code = payload.get("sku_code")
    if not sku_code:
        logger.warning("Procurement: wms.stock.low без sku_code — пропуск (%s)", payload)
        return
    from modules.procurement.routes import _request_from_deficit

    await _request_from_deficit(
        ctx.session,
        sku_code=sku_code,
        sku_title=payload.get("sku_title") or sku_code,  # фриз: item := sku_title (fallback sku_code)
        warehouse=payload.get("warehouse") or "Главный",
        deficit=payload.get("deficit") or 0,
        reorder_qty=payload.get("reorder_qty") or 0,
    )
    logger.info(
        "Procurement: автозаявка по дефициту — %s (%s, дефицит %s)",
        payload.get("sku_title") or sku_code,
        payload.get("warehouse"),
        payload.get("deficit"),
    )


async def on_ship_deadline_set(payload: dict, ctx) -> None:
    """Срок отгрузки клиенту из продаж → требования закупок по (сделка, sku) (sales → procurement).

    Закупки планируют машину к самому раннему сроку позиций − буфер «последней мили», иначе риск
    срыва отгрузки и штрафа. Идемпотентно: upsert по (deal_id, sku_code); повторный сигнал
    обновляет срок/штраф; позиции, убранные из сделки, снимаются (только если items не пуст —
    защита от случайного стирания). Обработчик с ``(payload, ctx)``: коммит делает relay, не здесь.
    """
    if ctx is None:
        return
    from sqlalchemy import select

    from modules.procurement.models import ShipRequirement
    from modules.procurement.plan import parse_deadline

    deal_id = payload.get("deal_id")
    if deal_id is None:
        logger.warning("Procurement: ship_deadline.set без deal_id — пропуск (%s)", payload)
        return
    raw = payload.get("ship_deadline")
    parsed = parse_deadline(raw)
    items = payload.get("items") or []
    existing = {
        r.sku_code: r
        for r in (await ctx.session.execute(
            select(ShipRequirement).where(ShipRequirement.deal_id == deal_id)
        )).scalars().all()
    }
    seen: set[str] = set()
    for it in items:
        sku = (it.get("sku_code") or "").strip()
        if not sku:
            continue
        seen.add(sku)
        fields = dict(
            number=payload.get("number") or "",
            counterparty=payload.get("counterparty") or "",
            qty=Decimal(str(it.get("qty") or 0)),
            ship_deadline=raw,
            ship_deadline_date=parsed,
            penalty_rate_pct=_dec(payload.get("penalty_rate_pct")),
            penalty_cap_pct=_dec(payload.get("penalty_cap_pct")),
            penalty_terms=payload.get("penalty_terms"),
        )
        row = existing.get(sku)
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
        else:
            row = ShipRequirement(deal_id=deal_id, sku_code=sku, **fields)
            ctx.session.add(row)
            existing[sku] = row  # дубль того же sku в items → обновим эту строку (UNIQUE deal_id+sku_code)
    if items:  # позиции, убранные из сделки → снять требование (но не стираем на пустом сигнале)
        for sku, row in existing.items():
            if sku not in seen:
                await ctx.session.delete(row)
    logger.info(
        "Procurement: срок клиента по сделке %s → %s (%d поз.)",
        payload.get("number"), raw, len(seen),
    )


async def _affected_skus(session, ref_key: str, key: str) -> list[str]:
    """Коды SKU, затронутые сменой справочника: ``core.skus`` → сам товар; ``core.tnved`` → товары
    с этим (своим) кодом ТН ВЭД. Групповое наследование ТН ВЭД — отложенный каскад (# ponytail:
    резолв через группы номенклатуры, если понадобится точность по наследованию)."""
    from sqlalchemy import select

    from core.domain.models import Sku

    if ref_key == "core.skus":
        return [key]
    if ref_key == "core.tnved":
        return list(
            (await session.execute(select(Sku.code).where(Sku.tnved_code == key))).scalars().all()
        )
    return []


async def on_reference_changed(payload: dict, ctx) -> None:
    """Смена справочной ставки/мастер-поля SKU → пересчёт плановой landed затронутых товаров
    (reference → procurement, REF3-7 / круг 4 B2).

    Шина без wildcard, поэтому подписка на КОНКРЕТНЫЕ события: ``reference.ref_tnved.changed``
    (пошлина) и ``reference.sku.changed`` (мастер-поля товара). НДС/курс — доля Финансов (НДС
    возвратный — не входит в landed; товар заказа уже в BYN — курс не двигает BYN-landed).

    ⚠️ Дебаунс против каскада: массовая правка справочника = шквал событий. В пределах одного
    прохода relay каждый SKU пересчитываем не более раза — кэш на ``ctx`` (relay переиспользует
    один ``EventContext`` на весь батч). Коммит делает relay, не обработчик (§2.5).
    """
    if ctx is None:
        return
    ref_key = payload.get("ref_key") or ""
    entity_ref = payload.get("entity_ref") or ""
    key = entity_ref.split(":", 1)[1] if ":" in entity_ref else ""
    if not key:
        return
    codes = await _affected_skus(ctx.session, ref_key, key)
    if not codes:
        return
    from modules.procurement.routes import _recompute_estimated_landed

    done = getattr(ctx, "_procurement_ref_recomputed", None)
    if done is None:
        done = set()
        ctx._procurement_ref_recomputed = done  # дедуп пересчётов за один проход relay
    fresh = [c for c in codes if c not in done]
    for code in fresh:
        done.add(code)
        await _recompute_estimated_landed(ctx.session, code)
    logger.info(
        "Procurement: reference %s → пересчёт плановой landed (%d из %d, остальное — дедуп)",
        ref_key, len(fresh), len(codes),
    )
