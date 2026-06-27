# Модуль procurement — контекст для Claude

**Тип:** git submodule → ZAK-3 (правка = коммит в этот репозиторий)
**API-префикс:** `/procurement`
**Схема БД:** `procurement`
**Статус:** наполнен (воронка закупок; справочник поставщиков + scorecard; RFQ/тендер; заказы PO с машиной состояний, ETA, приходом на склад по позициям; landed cost наружу через фасад + предв. себес «Расчёт Китай»; претензии авто+ручные; без workflow/permissions)

## Назначение
Управление закупками (sourcing): ведение заявок на закупку по канбан-воронке от
потребности до завершения. Каждая заявка — товар + поставщик + сумма + стадия
sourcing-цикла. При переходе в стадию «Приёмка / QC» закупка публикует событие
прихода товара на склад (procurement → wms).

## Файлы
- `module.py` — `ProcurementModule(ModuleContract)` + фабрика `get_module()`; регистрация роутера, подписки `production.scrap`, виджета и **фасада себестоимости** (`core.services.landed_cost = LandedCostService()`).
- `models.py` — ORM-модели `PurchaseRequest`, `SupplierClaim`, `PurchaseOrder`, `PurchaseOrderLine`, `LandedCost`, `Supplier`, `Rfq`, `RfqBid` (схема `procurement`) + константы статусов заказа (`ORDER_STATUSES`/`OPEN_ORDER_STATUSES`/`RECEIVED_ORDER_STATUS`/`ORDER_RANK`).
- `schemas.py` — Pydantic-схемы воронки/заказа/позиций/претензий + `Supplier{Create,Update,Out}`, `Rfq{Create,Out}`/`RfqBid{In,Out}`/`RfqAward`, `SupplierClaimCreate`, `PurchaseOrderHeaderUpdate`, `CostEstimate*`.
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
- **Публикует** (emit): `procurement.received` — (1) при PATCH-смене **стадии воронки** на `qc`,
  payload `{item, qty, warehouse, entity_ref:"purchase:<id>"}`; (2) при приёмке **заказа** (`received`)
  ПО КАЖДОЙ позиции, payload `{sku_code, qty (float), warehouse:"Главный", entity_ref:
  "purchase_order:<id>:<line_id>", unit_landed_cost_byn (str)}`. Для wms (приход на склад; sku из `sku_code`/`item`).
- **Публикует** (emit): `procurement.landed_cost.calculated` — на приёмке заказа, по каждой
  номенклатуре. payload: `{sku_code, unit_landed_cost_byn (str), qty (str), total_landed_byn (str),
  shipment_id, stage, purchase_order_id, fx_*, entity_ref:"purchase_order:<id>"}`. Push для sales
  (снапшот себестоимости) + finance (landed-маржа unit×qty).
- **Публикует** (emit): `procurement.order.status_changed` — на каждом переходе статуса заказа.
  payload: `{order_id, number, from, to, supplier_id}`.
- **Публикует** (emit): `procurement.rfq.awarded` — при выборе победителя тендера.
  payload: `{rfq_id, supplier_id, price_byn (str), sku_code, entity_ref:"rfq:<id>"}`.
- **Публикует** (emit): `procurement.claim.resolved` — при закрытии претензии (resolved/rejected).
  payload: `{claim_id, supplier_id, claim_type, amount_byn (str|None), resolution, status,
  entity_ref:"claim:<id>"}`. Для finance/качества.
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
  - `id` (PK), `number` (авто `PO-2026-NNNN`), `supplier` (легаси-строка), `supplier_id` (int|None,
    soft-ref на `supplier`), `status` (str≤16, def `draft`), `eta_date` (Date|None — ETA),
    `freight_byn` (Numeric(14,2)), `created_at`.
  - **Машина состояний** `ORDER_RANK`: `draft`→`ordered`→`shipped`→`customs`→`received` (+`cancelled`).
    Вперёд (со скипами) можно, назад — 422; отмена из открытых, не из принятого (409). Валидирует
    `_validate_transition`. **Открытый** (`OPEN_ORDER_STATUSES = ordered/shipped/customs`) = в пути →
    sales вычитает в нетто-доступности (draft/received/cancelled — НЕ открыт). `received` фиксирует landed cost.
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
- **`supplier_claim`** (`SupplierClaim`) — претензия поставщику:
  - `id` (PK), `supplier` (легаси-строка), `supplier_id` (int|None, soft-ref), `item`, `reason`,
    `order_code`, `claim_type` (брак/недопоставка/пересорт/срок), `qty_affected` (int),
    `amount_byn` (Numeric|None — заявленная сумма), `resolution` (str — как урегулировано),
    `status` (`open`→`resolved`/`rejected`), `source` (`production` авто / `manual` ручная),
    `entity_ref`, `created_at`.
  - Создаётся авто (`on_production_scrap` при браке в ОТК) ИЛИ вручную (`POST /claims`).
- **`supplier`** (`Supplier`) — профиль поставщика закупок (НЕ дубль контрагента, эталон в MDM):
  - `id` (PK), `name`, `unp` (soft-ref на MDM-контрагента, провенанс), `country`/`flag`,
    `contact_person`/`phone`/`email`, `payment_terms`, `lead_time_days` (int|None), `incoterms`,
    `status` (active/blocked), `notes`, `created_at`.
- **`rfq`** (`Rfq`) — запрос цен/тендер: `id`, `item`, `sku_code` (soft-ref), `qty`, `request_id`
  (soft-ref на purchase_request), `status` (open/awarded/cancelled), `due_date`, `created_at`.
- **`rfq_bid`** (`RfqBid`) — предложение поставщика: `id`, `rfq_id` (FK → rfq, CASCADE),
  `supplier_id` (soft-ref), `price_byn`, `lead_time_days`, `incoterms`, `note`, `is_winner` (bool),
  `created_at`. Индекс `ix_rfq_bid_rfq (rfq_id)`.
- **`purchase_request`** также получил `supplier_id` (int|None, soft-ref, приоритетный над строкой `supplier`).
- Схема таблиц `supplier`/`rfq`/`rfq_bid` + колонки `supplier_id`/поля претензий + PO default `draft` — миграция `0065`.

## API-эндпоинты (ключевые)
- `GET /procurement/requests` — плоский список заявок (сортировка по `id` desc), `list[PurchaseRequestOut]`.
- `GET /procurement/board` — воронка: заявки сгруппированы по стадиям через `build_board(STAGES, rows, _to_card)`, `FunnelBoardOut`.
- `POST /procurement/requests` — создать закупку (201); номер генерируется, если не задан.
- `PATCH /procurement/requests/{req_id}` — сменить стадию; при `stage == "qc"` эмитит `procurement.received` (404 если не найдена).
- `POST /procurement/orders` — создать заказ с позициями (201); номер `PO-2026-NNNN`, если не задан.
- `GET /procurement/orders` — все заказы с позициями (новые первыми, `list[PurchaseOrderOut]`).
- `GET /procurement/open-orders` — открытые заказы (статусы `ordered`/`shipped`/`customs`) с ETA и позициями, ближайший ETA первым — для расчёта «в пути» в sales.
- `GET /procurement/orders/{order_id}` — один заказ с позициями (для редактора машины).
- `PATCH /procurement/orders/{order_id}` — сменить статус по машине состояний (422 на откат назад /
  недопустимый, 409 на отмену принятого); эмит `procurement.order.status_changed`; при **фактической**
  приёмке (`received`) фиксирует landed cost + эмитит `landed_cost.calculated` + `procurement.received` по позициям.
- `PATCH /procurement/orders/{order_id}/header` — править шапку (фрахт/ETA/поставщик); 409 на принятом.
- `POST /procurement/orders/{order_id}/lines` / `DELETE …/lines/{line_id}` — добавить/убрать позицию (редактор машины; 409 на принятом).
- `GET /procurement/orders/{order_id}/landed-preview` — предпросмотр распределения landed cost БЕЗ фиксации (live-пересчёт; reuse `allocate_landed_cost`).
- `POST /procurement/cost-estimate` — предв. себестоимость импорта (Китай) по позициям; чистый расчёт без БД; цена/наценка/НДС НЕ считаются (полоса «Маржа»).
- `GET/POST /procurement/suppliers`, `GET/PATCH /procurement/suppliers/{id}` — справочник поставщиков (CRUD; 404).
- `GET /procurement/suppliers/{id}/scorecard` — балл поставщика 0–10 (заказы/претензии/выигранные RFQ; своевременность honest-empty).
- `GET/POST /procurement/rfq`, `GET /procurement/rfq/{id}` — тендер (предложения сортированы по цене, `best_bid_id`).
- `POST /procurement/rfq/{id}/bids` — предложение поставщика (409 если RFQ закрыт).
- `POST /procurement/rfq/{id}/award` — выбрать победителя (`is_winner`, status=awarded, эмит `rfq.awarded`).
- `GET /procurement/claims` — претензии (`list[SupplierClaimOut]`, новые первыми).
- `POST /procurement/claims` — ручное заведение претензии (source=manual).
- `PATCH /procurement/claims/{claim_id}` — назначить поставщика / урегулировать; при resolved/rejected — эмит `claim.resolved` (404 если не найдена).

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
- В `_to_card` балл поставщика — **реальный** (`_board_scores`: по заказам/претензиям через `supplier_id`),
  пусто если поставщик не связан или нет данных. Своевременность пока не входит (нет дат факта приёмки).
- `amount` в схемах — `float`, но в модели `Numeric`; в POST приводится `Decimal(str(...))`.
- **`PurchaseOrder` ≠ `PurchaseRequest`:** воронка — пред-заказный sourcing; заказ — уже размещённый,
  с позициями/ETA/статусом. Связи между ними пока нет (плоско). **Оба** эмитят `procurement.received` в
  WMS: воронка на `qc` (одной строкой), заказ на `received` (по каждой позиции) — следить, чтобы один
  физический приход не задвоился (если PR и PO ведут один товар — принимать в одном из путей).
- **Скоркарта**: своевременность/цена — honest-empty (нет дат факта приёмки заказа / нет эталона цены);
  балл = взвешенное среднее доступных компонент (сейчас только quality). `# ponytail:` веса захардкожены.
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