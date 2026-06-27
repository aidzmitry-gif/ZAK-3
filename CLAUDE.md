# Модуль procurement — контекст для Claude

**Тип:** git submodule → ZAK-3 (правка = коммит в этот репозиторий)
**API-префикс:** `/procurement`
**Схема БД:** `procurement`
**Статус:** частично наполнен (CRUD/воронка закупок + претензии поставщикам с автосозданием из брака производства + landed cost наружу через фасад ядра; без workflow/permissions)

## Назначение
Управление закупками (sourcing): ведение заявок на закупку по канбан-воронке от
потребности до завершения. Каждая заявка — товар + поставщик + сумма + стадия
sourcing-цикла. При переходе в стадию «Приёмка / QC» закупка публикует событие
прихода товара на склад (procurement → wms).

## Файлы
- `module.py` — `ProcurementModule(ModuleContract)` + фабрика `get_module()`; регистрация роутера, подписки `production.scrap`, виджета и **фасада себестоимости** (`core.services.landed_cost = LandedCostService()`).
- `models.py` — ORM-модели `PurchaseRequest`, `SupplierClaim`, `PurchaseOrder`, `PurchaseOrderLine`, `LandedCost` (схема `procurement`).
- `schemas.py` — Pydantic-схемы: `PurchaseRequestCreate/Out`, `StageUpdate`, `PurchaseOrderCreate/Out`, `PurchaseOrderLineIn/Out`, `PurchaseOrderStatusUpdate`, `SupplierClaimOut`, `SupplierClaimUpdate`.
- `landed_cost.py` — `LandedCostService` (реализация `core.services.landed_cost.LandedCostGateway`): чтение последней себестоимости по `sku_code` (+ батч).
- `cost_estimate.py` — `estimate_china_cost` (чистая функция): предв. себестоимость импорта (Китай) per-line — цена+комиссия+страховка+фрахт+пошлина → landed BYN/шт (буфер курса ≥10%). НЕ дубль `allocate_landed_cost` (тот — распределение факт-издержек на приёмке).
- `events.py` — обработчик `on_production_scrap` (брак производства → претензия поставщику).
- `routes.py` — HTTP-API под `/procurement` + маппинг строки в `FunnelCard` + фиксация landed cost по позициям на приёмке заказа (`_fixate_landed_cost`).
- `stages.py` — список стадий воронки `STAGES` (id/title/color, порядок = колонки канбана).
- `__init__.py` — пустой пакет-маркер.

## Что регистрирует в ядре (из register())
- **Роуты:** `core.include_router(routes.router, prefix="/procurement")`.
- **Подписки:** `core.subscribe("production.scrap", events.on_production_scrap)` — брак → претензия.
- **Виджет:** `core.register_widget(Widget("procurement", "Закупки", source="procurement.requests"))`.
- **Фасад себестоимости:** `core.services.landed_cost = LandedCostService()` — продажи читают landed cost по `sku_code` через фасад ядра (CQRS, как `stock`/`onec`), не импортируя procurement.
- Workflow / permissions / roles / telegram — **не регистрируются**.

## События
- **Публикует** (emit): `procurement.received` — при PATCH-смене стадии на `qc` (приёмка/QC).
  payload: `{item, qty, warehouse: "Главный", entity_ref: "purchase:<id>"}`. Предназначено для wms (приход на склад).
- **Публикует** (emit): `procurement.landed_cost.calculated` — на приёмке заказа (`received`), по
  каждой номенклатуре. payload: `{sku_code, unit_landed_cost_byn (str), shipment_id, stage,
  purchase_order_id, fx_rate, fx_date, fx_rate_basis, entity_ref:"purchase_order:<id>"}`. Push-
  инвалидация снапшота себестоимости в sales (пересчёт маржи). Подписчиков пока нет (sales — позже).
- **Подписан на** (subscribe): `production.scrap` (брак в ОТК производства) → `on_production_scrap`
  открывает претензию поставщику (`SupplierClaim`, `status="open"`, поставщик пуст). Обработчик с
  `(payload, ctx)`: пишет в сессию relay, **коммит делает relay**, не обработчик.

Эмит — через `core.event_bus.emit(session, "procurement.received", {...})` в той же
транзакции (transactional outbox).

## Модель данных (таблицы схемы `procurement`)
- **`purchase_request`** (`PurchaseRequest`) — пред-заказная воронка sourcing:
  - `id` (PK), `number` (str, авто `ЗАК-2026-NNNN` если пусто), `supplier`, `flag` (str≤8),
    `item`, `qty` (int, def 1), `amount` (Numeric(14,2)), `priority` (str, def «Средний»),
    `owner`, `stage` (str, def `need`), `due_date` (str|None), `insight` (str≤400),
    `created_at` (server_default now()).
  - `stage` — строка, значения из `STAGES`: `need` → `sourcing` → `nego` → `analysis` →
    `approval` → `po` → `supply` → `qc` → `done`. Это **не** Enum БД, просто String(32).
- **`purchase_order`** (`PurchaseOrder`) — размещённый заказ поставщику («машина»/контейнер):
  - `id` (PK), `number` (авто `PO-2026-NNNN`), `supplier`, `status` (str≤16: `ordered`→`shipped`
    →`customs`→`received`), `eta_date` (Date|None — ETA), `freight_byn` (Numeric(14,2) — общий
    фрахт партии, разносится на позиции), `created_at`.
  - **Открытый** заказ (`OPEN_ORDER_STATUSES = ordered/shipped/customs`) = товар в пути → sales
    вычитает в нетто-доступности. Приёмка (`RECEIVED_ORDER_STATUS = received`) фиксирует landed cost.
- **`purchase_order_line`** (`PurchaseOrderLine`) — позиция заказа:
  - `id` (PK), `order_id` (FK → `purchase_order.id`, `ondelete=CASCADE`), `sku_code` (soft-ref),
    `qty` (Numeric(14,2)), `goods_value_byn` (Numeric(14,2) — стоимость товара позиции),
    `weight` (Numeric(14,3) — кг брутто), `volume` (Numeric(14,4) — м³). Вес/объём — базы
    распределения фрахта. Индекс `ix_purchase_order_line_order (order_id)`.
- **`landed_cost`** (`LandedCost`) — себестоимость единицы номенклатуры, доведённой до склада (BYN):
  - `id` (PK), `sku_code` (str≤64, soft-ref, без FK), `purchase_order_id` (int|None, soft-ref),
    `shipment_id` (str — PO/рейс, провенанс), `unit_landed_cost_byn` (Numeric(14,4)),
    `stage` (str: `estimated`/`actual`), `fx_rate`/`fx_date`/`fx_rate_basis` (курс, null в мин.срезе),
    `fixed_at` (now(), onupdate now()), `created_at` (now()).
  - `UNIQUE (sku_code, purchase_order_id)` (upsert на приёмке не плодит дубль) + индекс
    `ix_landed_cost_sku_fixed (sku_code, fixed_at)` (выборка «последняя по номенклатуре»).
  - Фиксируется на приёмке заказа через `_fixate_landed_cost`: агрегирует позиции по `sku_code`,
    разносит `freight_byn` (общий движок `allocate_landed_cost`, база — вес, иначе стоимость) →
    одна строка на номенклатуру, `stage="estimated"`. Пошлина ТН ВЭД / два FX / буфер +10% — Горизонт 2.
- **`supplier_claim`** (`SupplierClaim`) — претензия поставщику (миграция `0033`):
  - `id` (PK), `supplier` (str, пусто пока закупщик не привяжет), `item`, `reason`, `order_code`,
    `status` (str: `open` → `resolved`/`rejected`), `source` (str, `production` для авто-претензий брака),
    `entity_ref` (напр. `production:qc:<id>`), `created_at` (server_default now()).
  - Создаётся автоматически обработчиком `on_production_scrap` при браке в ОТК производства.

## API-эндпоинты (ключевые)
- `GET /procurement/requests` — плоский список заявок (сортировка по `id` desc), `list[PurchaseRequestOut]`.
- `GET /procurement/board` — воронка: заявки сгруппированы по стадиям через `build_board(STAGES, rows, _to_card)`, `FunnelBoardOut`.
- `POST /procurement/requests` — создать закупку (201); номер генерируется, если не задан.
- `PATCH /procurement/requests/{req_id}` — сменить стадию; при `stage == "qc"` эмитит `procurement.received` (404 если не найдена).
- `POST /procurement/orders` — создать заказ с позициями (201); номер `PO-2026-NNNN`, если не задан.
- `GET /procurement/orders` — все заказы с позициями (новые первыми, `list[PurchaseOrderOut]`).
- `GET /procurement/open-orders` — открытые заказы (статусы `ordered`/`shipped`/`customs`) с ETA и позициями, ближайший ETA первым — для расчёта «в пути» в sales.
- `PATCH /procurement/orders/{order_id}` — сменить статус; при **фактической** приёмке (`received`) фиксирует landed cost по позициям + эмитит `procurement.landed_cost.calculated` (404 если не найден). Повторная приёмка — без дубля (upsert).
- `POST /procurement/cost-estimate` — предв. себестоимость импорта (Китай) по позициям: вход `{rates, lines[]}` → `{lines[{…, unit_landed_cost_byn}], total_landed_byn}`. Чистый расчёт без БД (для калькулятора сделки/машины); цена/наценка/НДС НЕ считаются (полоса «Маржа»).
- `GET /procurement/claims` — претензии поставщикам (`list[SupplierClaimOut]`, новые первыми).
- `PATCH /procurement/claims/{claim_id}` — назначить поставщика / сменить статус (`SupplierClaimUpdate`; 404 если не найдена).

## Межмодульные связи и зависимости
- **procurement → wms:** событие `procurement.received` (приход на склад). Прямых вызовов других модулей нет.
- **procurement → sales (фасад):** себестоимость наружу через `core.services.landed_cost`
  (read-only, по `sku_code`). Sales читает для расчёта маржи (`quote_line`) и хранит снапшот;
  записи sales → procurement нет. Канон: `coordination/procurement-costing-handoff.md`.
- **production → procurement:** событие `production.scrap` (брак в ОТК) → автопретензия `SupplierClaim`.
- Использует ядро: `core.runtime.funnel` (`FunnelBoardOut`, `FunnelCard`, `build_board`),
  `core.runtime.deps` (`get_core`, `get_session`), `core.runtime.core.Core`, `core.db.base.Base`.

## Подводные камни / важные детали
- **POST /requests коммитит сам** (`session.commit()` внутри роута), при создании делает
  `flush` → присваивает авто-`number` (`ЗАК-2026-{id:04d}`) → второй commit. PATCH тоже коммитит сам.
- `RECEIVED_STAGE = "qc"` (константа в `routes.py`): именно эта стадия = физический приём
  товара и триггер прихода на склад, а не финальная `done`.
- В `_to_card` «Supplier Score 8.7» — захардкоженная заглушка, показывается только на стадиях `nego`/`analysis`.
- `amount` в схемах — `float`, но в модели `Numeric`; в POST приводится `Decimal(str(...))`.
- **`PurchaseOrder` ≠ `PurchaseRequest`:** воронка (`PurchaseRequest`) — пред-заказный sourcing;
  заказ (`PurchaseOrder`) — уже размещённый, с позициями/ETA/статусом. Связи между ними пока нет
  (плоско). Приёмка заказа (`received`) НЕ эмитит `procurement.received` в WMS — это делает воронка
  (`qc`); сшивка PO→WMS-приход (эмит по позициям) — Горизонт 2, согласовать со Складом.
- **landed cost — мин.срез:** позиция без `sku_code` или с `qty<=0` пропускается (себестоимость
  единицы не определена — не пишем `unit=0`, чтобы не замаскировать дыру в марже); издержки = только `freight_byn`
  (база — вес, иначе стоимость). Пошлина по ТН ВЭД, два FX-курса и буфер +10% (методика «Расчёт
  Китай») — Горизонт 2. Себестоимость храним в 4 знака, но движок `allocate_landed_cost` отдаёт
  единицу в копейках. **Результат — в СВОЮ таблицу `procurement.landed_cost`, НЕ в
  `integrations.batch.unit_landed_cost`** (это схема СИНК — не трогать).
- `last_landed_cost` → `None` при отсутствии строки (НЕ 0 — иначе спрятали бы дыру в марже).
- Модель не добавляет `core.subscribe`/workflow/permissions — функциональность: CRUD/воронка + заказы с landed cost + фасад себестоимости.

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