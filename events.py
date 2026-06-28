"""Обработчики событий модуля Procurement."""
from __future__ import annotations

import logging

logger = logging.getLogger("aios.procurement")


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
