"""График сбора машины (Китай → Минск): этапы, шаблоны длительностей, обратный waterfall.

Чистая логика без БД/ORM (легко тестируется). Модель из реального плана заказчика
(«Перевозка план»): каждая машина проходит фиксированную цепочку этапов; длительность
каждого этапа зависит от способа перевозки (Контейнер ≈112 дн / Машина ≈83 дн — разница
в производстве и транзите до Минска). План считается ОТ дедлайна «В Минске до» НАЗАД:
дата окончания этапа = дата окончания следующего − длительность следующего.
"""
from __future__ import annotations

from datetime import date, timedelta

# Этапы в порядке прохождения (id, человекочитаемое название). Каждый этап = его ОКОНЧАНИЕ
# (дедлайн): «Сбор до», «оплата», … Последний (customs) завершается в дату «В Минске до».
SHIPMENT_STAGES: list[tuple[str, str]] = [
    ("collection", "Сбор заказов"),
    ("payment", "Оплата / выкуп"),
    ("production", "Производство"),
    ("to_cn_warehouse", "Доставка до склада (Китай)"),
    ("to_minsk", "Доставка до Минска"),
    ("customs", "Растоможка и подготовка"),
]
STAGE_TITLES = dict(SHIPMENT_STAGES)
STAGE_ORDER = [code for code, _ in SHIPMENT_STAGES]

# Шаблоны длительностей по способу перевозки (дни на этап). Источник — план заказчика.
# Это дефолты-сиды; способ перевозки редактируется (своя строка в transport_method).
DEFAULT_METHODS: dict[str, dict] = {
    "container": {
        "name": "Контейнер",
        "durations": {"collection": 28, "payment": 7, "production": 21,
                      "to_cn_warehouse": 7, "to_minsk": 42, "customs": 7},  # Σ 112
    },
    "truck": {
        "name": "Машина",
        "durations": {"collection": 28, "payment": 7, "production": 14,
                      "to_cn_warehouse": 7, "to_minsk": 20, "customs": 7},  # Σ 83
    },
}


def total_transit_days(durations: dict[str, int]) -> int:
    """Итого транзитный срок (сумма длительностей всех этапов)."""
    return sum(int(durations.get(code, 0)) for code in STAGE_ORDER)


def build_milestone_plan(
    durations: dict[str, int], target_arrival: date
) -> tuple[list[dict], date]:
    """Обратный waterfall от «В Минске до» (``target_arrival``).

    Последний этап (``customs``) завершается в ``target_arrival``; дата окончания каждого
    предыдущего = дата окончания следующего − длительность следующего. Возвращает
    (список этапов в прямом порядке [{stage, seq, duration_days, planned_date}], дата старта
    «Спланирован заказ» = окончание сбора − длительность сбора). Неизвестный этап → 0 дней.
    """
    plans: list[dict] = []
    cursor = target_arrival
    for code in reversed(STAGE_ORDER):  # с конца: customs завершается в target
        days = int(durations.get(code, 0))
        plans.append({"stage": code, "duration_days": days, "planned_date": cursor})
        cursor = cursor - timedelta(days=days)
    plans.reverse()
    for seq, p in enumerate(plans):
        p["seq"] = seq
    start_date = cursor  # после полного отката — дата планирования заказа
    return plans, start_date
