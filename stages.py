"""Стадии воронки закупок (порядок списка = порядок колонок на канбане)."""

STAGES: list[dict] = [
    {"id": "need", "title": "Потребность", "color": "#3B82F6"},
    {"id": "sourcing", "title": "Поиск поставщиков", "color": "#8B5CF6"},
    {"id": "nego", "title": "Переговоры", "color": "#F59E0B"},
    {"id": "analysis", "title": "Анализ поставщика", "color": "#06B6D4"},
    {"id": "approval", "title": "Согласование", "color": "#EC4899"},
    {"id": "po", "title": "Заказ / PO", "color": "#14B8A6"},
    {"id": "supply", "title": "Производство", "color": "#6366F1"},
    {"id": "qc", "title": "Приёмка / QC", "color": "#F97316"},
    {"id": "done", "title": "Завершено", "color": "#22C55E"},
]
