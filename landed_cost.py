"""Реализация фасада себестоимости (``core.services.landed_cost``) на стороне владельца.

Закупки — источник истины landed cost. Этот сервис регистрируется в ядре
(``core.services.landed_cost = LandedCostService()``), а продажи читают себестоимость
через фасад, не импортируя procurement (CQRS, §6). Только чтение последней посчитанной
строки ``LandedCost`` по коду номенклатуры; ``None`` — нет закрытого расчёта (НЕ 0,
иначе спрятали бы дыру в марже).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.procurement.models import LandedCost


def _to_dict(row: LandedCost) -> dict:
    return {
        "unit_landed_cost_byn": row.unit_landed_cost_byn,
        "shipment_id": row.shipment_id,
        "fixed_at": row.fixed_at,
        "stage": row.stage,
        "fx_rate": row.fx_rate,
        "fx_date": row.fx_date,
        "fx_rate_basis": row.fx_rate_basis,
    }


class LandedCostService:
    """Чтение последней себестоимости номенклатуры (реализует ``LandedCostGateway``)."""

    async def last_landed_cost(self, session: AsyncSession, sku_code: str) -> dict | None:
        row = (
            await session.execute(
                select(LandedCost)
                .where(LandedCost.sku_code == sku_code)
                .order_by(LandedCost.fixed_at.desc(), LandedCost.id.desc())
                .limit(1)
            )
        ).scalars().first()
        return _to_dict(row) if row is not None else None

    async def last_landed_cost_batch(
        self, session: AsyncSession, sku_codes: list[str]
    ) -> dict[str, dict | None]:
        # ключ для КАЖДОГО входного кода (None, если строки нет) — иначе каталог-пикер
        # упадёт на result[code]. Один запрос, первая строка на код = последняя (order desc).
        result: dict[str, dict | None] = {code: None for code in sku_codes}
        if not result:
            return result
        rows = (
            await session.execute(
                select(LandedCost)
                .where(LandedCost.sku_code.in_(result.keys()))
                .order_by(LandedCost.fixed_at.desc(), LandedCost.id.desc())
            )
        ).scalars().all()
        for row in rows:
            if result[row.sku_code] is None:  # первая встреченная = последняя по fixed_at
                result[row.sku_code] = _to_dict(row)
        return result
