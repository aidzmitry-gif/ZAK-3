"""Модуль Procurement (Закупки) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.procurement import routes


class ProcurementModule(ModuleContract):
    name = "procurement"
    version = "0.1.0"
    api_prefix = "/procurement"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.register_widget(Widget("procurement", "Закупки", source="procurement.requests"))


def get_module() -> ModuleContract:
    return ProcurementModule()
