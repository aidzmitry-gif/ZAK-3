"""Модуль Procurement (Закупки) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.procurement import events, routes
from modules.procurement.additional_expense_routes import router as additional_expense_router
from modules.procurement.deal_demands import router as deal_demands_router
from modules.procurement.expected_reservations import router as expected_reservations_router
from modules.procurement.landed_cost import LandedCostService
from modules.procurement.order_creation import router as order_creation_router
from modules.procurement.order_edit_commands import router as order_edit_router
from modules.procurement.ownership import router as ownership_router
from modules.procurement.receipt_documents import router as receipt_router
from modules.procurement.rfq_scoped import router as rfq_scoped_router
from modules.procurement.scoped_claims import router as scoped_claims_router
from modules.procurement.scoped_reads import router as scoped_reads_router
from modules.procurement.source_gateway import ProcurementSourceService
from modules.procurement.supplier_contracts import router as supplier_contract_router


class ProcurementModule(ModuleContract):
    name = "procurement"
    version = "0.1.0"
    api_prefix = "/procurement"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.include_router(receipt_router, prefix=self.api_prefix)
        core.include_router(supplier_contract_router, prefix=self.api_prefix)
        core.include_router(additional_expense_router, prefix=self.api_prefix)
        core.include_router(deal_demands_router, prefix=self.api_prefix)
        core.include_router(expected_reservations_router, prefix=self.api_prefix)
        core.include_router(ownership_router, prefix=self.api_prefix)
        core.include_router(order_creation_router, prefix=self.api_prefix)
        core.include_router(order_edit_router, prefix=self.api_prefix)
        core.include_router(scoped_reads_router, prefix=self.api_prefix)
        core.include_router(rfq_scoped_router, prefix=self.api_prefix)
        core.include_router(scoped_claims_router, prefix=self.api_prefix)
        # брак в производстве → автопретензия поставщику (production → procurement, §2.5)
        core.subscribe("production.scrap", events.on_production_scrap)
        # дефицит склада → авто-черновик заявки на закупку (wms → procurement, MRP-lite, круг 3)
        core.subscribe("wms.stock.low", events.on_stock_low)
        # срок отгрузки клиенту из продаж → требования к плану машины (sales → procurement)
        core.subscribe("sales.deal.ship_deadline.set", events.on_ship_deadline_set)
        core.subscribe("sales.deal.loss_finalized", events.on_deal_loss_finalized)
        # подтверждённый WMS резерв закрывает только принятую часть ожидаемого
        # клиентского резерва; складские движения остаются ответственностью WMS
        core.subscribe("sales.stock.reserved", events.on_stock_reserved_for_expected)
        # QC-приёмка по связанной первичной накладной становится отдельным
        # источником физически принятого количества для ожидаемого резерва.
        core.subscribe("wms.receipt.accepted", events.on_physical_receipt_accepted)
        # смена справочников (пошлина ТН ВЭД / мастер-поля SKU) → пересчёт плановой landed
        # (reference → procurement, круг 4 B2; шина без wildcard — подписка по конкретным типам)
        core.subscribe("reference.ref_tnved.changed", events.on_reference_changed)
        core.subscribe("reference.sku.changed", events.on_reference_changed)
        core.register_widget(Widget("procurement", "Закупки", source="procurement.requests"))
        # себестоимость партии наружу — sales читает через фасад для расчёта маржи (§6)
        core.services.landed_cost = LandedCostService()
        core.services.procurement_source = ProcurementSourceService()


def get_module() -> ModuleContract:
    return ProcurementModule()
