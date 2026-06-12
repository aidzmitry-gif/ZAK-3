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
