"""Модуль Procurement (Закупки) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.procurement import events, routes
from modules.procurement.landed_cost import LandedCostService


class ProcurementModule(ModuleContract):
    name = "procurement"
    version = "0.1.0"
    api_prefix = "/procurement"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        # брак в производстве → автопретензия поставщику (production → procurement, §2.5)
        core.subscribe("production.scrap", events.on_production_scrap)
        # дефицит склада → авто-черновик заявки на закупку (wms → procurement, MRP-lite, круг 3)
        core.subscribe("wms.stock.low", events.on_stock_low)
        # срок отгрузки клиенту из продаж → требования к плану машины (sales → procurement)
        core.subscribe("sales.deal.ship_deadline.set", events.on_ship_deadline_set)
        # смена справочников (пошлина ТН ВЭД / мастер-поля SKU) → пересчёт плановой landed
        # (reference → procurement, круг 4 B2; шина без wildcard — подписка по конкретным типам)
        core.subscribe("reference.ref_tnved.changed", events.on_reference_changed)
        core.subscribe("reference.sku.changed", events.on_reference_changed)
        core.register_widget(Widget("procurement", "Закупки", source="procurement.requests"))
        # себестоимость партии наружу — sales читает через фасад для расчёта маржи (§6)
        core.services.landed_cost = LandedCostService()


def get_module() -> ModuleContract:
    return ProcurementModule()
