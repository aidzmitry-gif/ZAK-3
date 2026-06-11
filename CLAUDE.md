# Модуль procurement — контекст для Claude

**Тип:** git submodule → ZAK-3 (правка = коммит в этот репозиторий)
**API-префикс:** `/procurement`
**Схема БД:** `procurement`
**Статус:** частично наполнен (одна модель + CRUD/воронка; без workflow, permissions, событийных подписок)

## Назначение
Управление закупками (sourcing): ведение заявок на закупку по канбан-воронке от
потребности до завершения. Каждая заявка — товар + поставщик + сумма + стадия
sourcing-цикла. При переходе в стадию «Приёмка / QC» закупка публикует событие
прихода товара на склад (procurement → wms).

## Файлы
- `module.py` — `ProcurementModule(ModuleContract)` + фабрика `get_module()`; регистрация роутера и виджета.
- `models.py` — ORM-модель `PurchaseRequest` (схема `procurement`).
- `schemas.py` — Pydantic-схемы: `PurchaseRequestCreate`, `PurchaseRequestOut`, `StageUpdate`.
- `routes.py` — HTTP-API под `/procurement` + маппинг строки в `FunnelCard`.
- `stages.py` — список стадий воронки `STAGES` (id/title/color, порядок = колонки канбана).
- `__init__.py` — пустой пакет-маркер.

## Что регистрирует в ядре (из register())
- **Роуты:** `core.include_router(routes.router, prefix="/procurement")`.
- **Виджет:** `core.register_widget(Widget("procurement", "Закупки", source="procurement.requests"))`.
- Workflow / permissions / roles / telegram / подписки на события — **не регистрируются**.

## События
- **Публикует** (emit): `procurement.received` — при PATCH-смене стадии на `qc` (приёмка/QC).
  payload: `{item, qty, warehouse: "Главный", entity_ref: "purchase:<id>"}`. Предназначено для wms (приход на склад).
- **Подписан на** (subscribe): нет.

Эмит — через `core.event_bus.emit(session, "procurement.received", {...})` в той же
транзакции (transactional outbox).

## Модель данных (таблицы схемы `procurement`)
- **`purchase_request`** (`PurchaseRequest`):
  - `id` (PK), `number` (str, авто `ЗАК-2026-NNNN` если пусто), `supplier`, `flag` (str≤8),
    `item`, `qty` (int, def 1), `amount` (Numeric(14,2)), `priority` (str, def «Средний»),
    `owner`, `stage` (str, def `need`), `due_date` (str|None), `insight` (str≤400),
    `created_at` (server_default now()).
  - `stage` — строка, значения из `STAGES`: `need` → `sourcing` → `nego` → `analysis` →
    `approval` → `po` → `supply` → `qc` → `done`. Это **не** Enum БД, просто String(32).

## API-эндпоинты (ключевые)
- `GET /procurement/requests` — плоский список заявок (сортировка по `id` desc), `list[PurchaseRequestOut]`.
- `GET /procurement/board` — воронка: заявки сгруппированы по стадиям через `build_board(STAGES, rows, _to_card)`, `FunnelBoardOut`.
- `POST /procurement/requests` — создать закупку (201); номер генерируется, если не задан.
- `PATCH /procurement/requests/{req_id}` — сменить стадию; при `stage == "qc"` эмитит `procurement.received` (404 если не найдена).

## Межмодульные связи и зависимости
- **procurement → wms:** событие `procurement.received` (приход на склад). Прямых вызовов других модулей нет.
- Использует ядро: `core.runtime.funnel` (`FunnelBoardOut`, `FunnelCard`, `build_board`),
  `core.runtime.deps` (`get_core`, `get_session`), `core.runtime.core.Core`, `core.db.base.Base`.

## Подводные камни / важные детали
- **POST /requests коммитит сам** (`session.commit()` внутри роута), при создании делает
  `flush` → присваивает авто-`number` (`ЗАК-2026-{id:04d}`) → второй commit. PATCH тоже коммитит сам.
- `RECEIVED_STAGE = "qc"` (константа в `routes.py`): именно эта стадия = физический приём
  товара и триггер прихода на склад, а не финальная `done`.
- В `_to_card` «Supplier Score 8.7» — захардкоженная заглушка, показывается только на стадиях `nego`/`analysis`.
- `amount` в схемах — `float`, но в модели `Numeric`; в POST приводится `Decimal(str(...))`.
- Модель не добавляет `core.subscribe`/workflow/permissions — функциональность ограничена CRUD+воронкой.

## Планируемая функциональность

- **Автоматический расчёт себестоимости импорта (landed cost)** — см.
  [docs/landed-cost.md](docs/landed-cost.md). Сейчас себестоимость прихода (импорт из
  Китая) закупщик вносит в 1С вручную; в ERP она считается автоматически из инвойса +
  всех сопутствующих расходов (фрахт, таможня, сертификация и т.д.), распределённых на
  номенклатуру. Расчёт финализируется на стадии `qc` (Приёмка), результат уходит в
  `wms` (оприходование по себестоимости), `integrations`/1С (вместо ручного ввода) и в
  ценообразование (база для цены продажи). Новые таблицы `purchase_invoice` /
  `landed_cost_expense` / `landed_cost_allocation`, событие `procurement.landed_cost.calculated`.
  **Связанная задача (отдельно): разработать технологию расчёта цен продажи** (наценка от landed cost).