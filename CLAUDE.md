# Модуль procurement — контекст для Claude

**Тип:** git submodule → ZAK-3 (правка = коммит в этот репозиторий)
**API-префикс:** `/procurement`
**Схема БД:** `procurement`
**Статус:** наполнен (воронка закупок; справочник поставщиков + scorecard со своевременностью; RFQ/тендер с авто-черновиком PO; заказы PO с машиной состояний, ETA, приходом на склад по позициям; landed cost наружу через фасад (estimated→actual на приёмке) + предв. себес «Расчёт Китай»; претензии авто+ручные; **реактивный MRP-lite: `wms.stock.low` → авто-черновик заявки**; **план сбора машины: этапы Китай→Минск, обратный waterfall от «В Минске до» (или от срока клиента из продаж − буфер), шаблон длительностей по способу перевозки, риск срыва/штрафа**; без workflow/permissions)

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
- `plan.py` — план сбора машины (чистая логика): `SHIPMENT_STAGES` (6 этапов Китай→Минск), `DEFAULT_METHODS` (Контейнер 112 дн / Машина 83 дн), `build_milestone_plan` (обратный waterfall от «В Минске до»), `total_transit_days`; срок клиента: `parse_deadline` (толерантный разбор даты-строки), `arrival_deadline` (срок − буфер), `LAST_MILE_BUFFER_DAYS` (3).
- `events.py` — обработчики событий: `on_production_scrap` (брак → претензия), `on_stock_low` (дефицит →
  автозаявка), `on_ship_deadline_set` (срок клиента → `ShipRequirement`), `on_reference_changed` (смена
  справочника → пересчёт плановой landed, с дебаунсом каскада через `ctx`).
- `routes.py` — HTTP-API под `/procurement` + маппинг строки в `FunnelCard` + фиксация landed cost по позициям на приёмке заказа (`_fixate_landed_cost`).
- `stages.py` — список стадий воронки `STAGES` (id/title/color, порядок = колонки канбана).
- `__init__.py` — пустой пакет-маркер.

## Что регистрирует в ядре (из register())
- **Роуты:** `core.include_router(routes.router, prefix="/procurement")`.
- **Подписки:** `core.subscribe("production.scrap", events.on_production_scrap)` — брак → претензия;
  `core.subscribe("wms.stock.low", events.on_stock_low)` — дефицит склада → авто-черновик заявки (MRP-lite);
  `core.subscribe("sales.deal.ship_deadline.set", events.on_ship_deadline_set)` — срок отгрузки клиенту → требование к плану машины.
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
  shipment_id, stage="actual", purchase_order_id, fx_*, entity_ref:"purchase_order:<id>"}`. Push для
  sales (снапшот себестоимости) + finance (landed-маржа unit×qty). `stage="actual"` — реальная
  приёмка (в отличие от планового cost-estimate); finance читает qty+total, по stage НЕ ветвится.
- **Публикует** (emit): `procurement.order.status_changed` — на каждом переходе статуса заказа.
  payload: `{order_id, number, from, to, supplier_id}`.
- **Публикует** (emit): `procurement.rfq.awarded` — при выборе победителя тендера.
  payload: `{rfq_id, supplier_id, price_byn (str), sku_code, entity_ref:"rfq:<id>"}`.
- **Публикует** (emit): `procurement.po.drafted` — при `award` тендера (выигранный RFQ → черновик PO).
  payload: `{po_ref, supplier_id, planned_amount (str BYN), currency:"BYN", eta_date (str|None),
  deal_id:null}`. Апстрим прогноза кэша для finance FIN-B1 (планируемый отток).
- **Публикует** (emit): `procurement.claim.resolved` — при закрытии претензии (resolved/rejected).
  payload: `{claim_id, supplier_id, claim_type, amount_byn (str|None), resolution, status,
  order_id (int|None), entity_ref:"claim:<id>"}`. Для finance/качества. `order_id` — резолв
  `order_code`→`PurchaseOrder.number` (None, если претензия не привязана к заказу).
- **Подписан на** (subscribe): `production.scrap` (брак в ОТК производства) → `on_production_scrap`
  открывает претензию поставщику (`SupplierClaim`, `status="open"`, поставщик пуст).
- **Подписан на** (subscribe): `wms.stock.low` (дефицит склада, плоский canonical payload) →
  `on_stock_low` создаёт авто-черновик заявки (`PurchaseRequest`, `origin="deficit"`, `stage="need"`)
  через `_request_from_deficit` (идемпотентно по позиции). Реактивный MRP-lite (wms → procurement).
- **Подписан на** (subscribe): `sales.deal.ship_deadline.set` (срок отгрузки клиенту + штраф + позиции) →
  `on_ship_deadline_set` складывает `ShipRequirement` по (сделка, sku) — крайняя дата клиента для плана
  машины (sales → procurement). Идемпотентно (upsert по deal_id+sku_code; снимает убранные позиции).
  Обработчики с `(payload, ctx)`: пишут в сессию relay, **коммит делает relay**, не обработчик.
- **Подписан на** (subscribe): `reference.ref_tnved.changed` + `reference.sku.changed` (смена пошлины ТН ВЭД /
  мастер-полей товара) → `on_reference_changed` пересчитывает ПЛАНОВУЮ (`estimated`) landed затронутых SKU
  по открытым заказам через фасад `core.services.sku_master.landed_inputs` (пошлина) + `allocate_landed_cost`
  (фрахт) — `_recompute_estimated_landed` (reference → procurement, круг 4 B2). **Шина без wildcard** —
  подписка по конкретным типам, не `reference.*.changed`. **Дебаунс против каскада:** в одном проходе relay
  каждый SKU пересчитывается не более раза (кэш на `ctx`). НДС/курс — доля Финансов (НДС возвратный, не в
  landed; товар PO уже в BYN — курс не двигает BYN-landed). Затронутые SKU: `core.skus`→сам товар,
  `core.tnved`→товары с этим (своим) кодом ТН ВЭД (групповое наследование — отложенный каскад).

Эмит — через `core.event_bus.emit(session, "procurement.received", {...})` в той же
транзакции (transactional outbox).

## Модель данных (таблицы схемы `procurement`)
- **`purchase_request`** (`PurchaseRequest`) — пред-заказная воронка sourcing:
  - `id` (PK), `number` (str, авто `ЗАК-2026-NNNN` если пусто), `supplier`, `flag` (str≤8),
    `item`, `qty` (int, def 1), `amount` (Numeric(14,2)), `priority` (str, def «Средний»),
    `owner`, `stage` (str, def `need`), `due_date` (str|None), `insight` (str≤400),
    `origin` (str≤32, def ''): `''` ручная / `'deficit'` автозаявка по сигналу дефицита склада,
    `created_at` (server_default now()).
  - `stage` — строка, значения из `STAGES`: `need` → `sourcing` → `nego` → `analysis` →
    `approval` → `po` → `supply` → `qc` → `done`. Это **не** Enum БД, просто String(32).
- **`purchase_order`** (`PurchaseOrder`) — размещённый заказ поставщику («машина»/контейнер):
  - `id` (PK), `number` (авто `PO-2026-NNNN`), `supplier` (легаси-строка), `supplier_id` (int|None,
    soft-ref на `supplier`), `status` (str≤16, def `draft`), `eta_date` (Date|None — ETA),
    `received_at` (DateTime|None — факт приёмки, ставится при переходе в `received`; основа
    своевременности scorecard), `freight_byn` (Numeric(14,2)), `created_at`.
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
    одна строка на номенклатуру, `stage="actual"` (реальная приёмка; плановая cost-estimate —
    `estimated`). Пошлина ТН ВЭД / два FX / буфер +10% — Горизонт 2.
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
- **`transport_method`** (`TransportMethod`) — справочник способов перевозки = шаблон длительностей
  этапов: `id`, `code` (unique: container/truck/…), `name`, `durations` (JSON {stage: дни}), `active`,
  `created_at`. Дефолты (Контейнер 112 / Машина 83) сидятся лениво из `plan.DEFAULT_METHODS`; редактируется.
- **`purchase_order_milestone`** (`PurchaseOrderMilestone`) — веха графика сбора машины: `id`,
  `order_id` (FK → purchase_order, CASCADE), `stage` (этап из `plan.SHIPMENT_STAGES`), `seq`,
  `duration_days` (правится на машине), `planned_date` (обратный waterfall), `actual_date` (факт).
  `UNIQUE (order_id, stage)` + индекс `ix_purchase_order_milestone_order`.
- **`purchase_order`** также получил `transport_method_code` (soft-ref на `transport_method`) +
  `target_arrival_date` (Date|None — «В Минске до», якорь обратного плана).
- Схема таблиц `supplier`/`rfq`/`rfq_bid` + колонки `supplier_id`/поля претензий + PO default `draft` — миграция `0065`.
- Колонки `purchase_request.origin` + `purchase_order.received_at` (круг 3, MRP-lite + своевременность) — миграция `0071`.
- Таблицы `transport_method`/`purchase_order_milestone` + `purchase_order.transport_method_code`/`target_arrival_date` (план сбора машины) — миграция `0072`.
- **`ship_requirement`** (`ShipRequirement`) — срок отгрузки клиенту по (сделка, sku) из продаж:
  `id`, `deal_id` (soft-ref на sales.deal), `number`, `counterparty`, `sku_code`, `qty`, `ship_deadline`
  (сырая строка), `ship_deadline_date` (разобранная дата), `penalty_rate_pct`/`penalty_cap_pct`/`penalty_terms`,
  `updated_at`. `UNIQUE (deal_id, sku_code)` + индекс `ix_ship_requirement_sku`. Наполняется из
  `sales.deal.ship_deadline.set`. Машина планируется к самому раннему сроку позиций − буфер. Миграция `0074`.

## API-эндпоинты (ключевые)
- `GET /procurement/requests` — плоский список заявок (сортировка по `id` desc), `list[PurchaseRequestOut]`.
- `GET /procurement/board` — воронка: заявки сгруппированы по стадиям через `build_board(STAGES, rows, _to_card)`, `FunnelBoardOut`.
- `POST /procurement/requests` — создать закупку (201); номер генерируется, если не задан.
- `POST /procurement/requests/from-deficit` — авто-черновик заявки из сигнала дефицита (тонкий
  debug/manual вход поверх `_request_from_deficit`; штатный путь — подписка `wms.stock.low`).
  Идемпотентно по (origin='deficit', позиция).
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
- `GET /procurement/transport-methods` / `PATCH …/{code}` — справочник способов перевозки (шаблон длительностей этапов; лениво сидится Контейнер/Машина; правка названия/длительностей/активности).
- `POST /procurement/orders/{order_id}/plan` — запланировать/пересчитать график сбора машины (способ перевозки + дедлайн «В Минске до»); этапы — обратный waterfall, факт (`actual_date`) сохраняется. Если дата не задана — авто-подсказка из самого раннего срока клиента − буфер (422, если нет ни даты, ни срока).
- `GET /procurement/orders/{order_id}/plan` — план сбора машины: этапы с план/факт датами + старт + итог дней + ограничение от срока клиента (`required_by`/`required_arrival`/`slack_days`/`at_risk`/`at_risk_deals` со штрафом).
- `POST /procurement/cost-estimate` — предв. себестоимость импорта (Китай) по позициям; чистый расчёт без БД; цена/наценка/НДС НЕ считаются (полоса «Маржа»).
- `GET/POST /procurement/suppliers`, `GET/PATCH /procurement/suppliers/{id}` — справочник поставщиков (CRUD; 404).
- `GET /procurement/suppliers/{id}/scorecard` — балл поставщика 0–10 (заказы/претензии/выигранные RFQ;
  своевременность — доля заказов, принятых не позже ETA по `received_at` vs `eta_date`; None без таких заказов).
- `GET/POST /procurement/rfq`, `GET /procurement/rfq/{id}` — тендер (предложения сортированы по цене, `best_bid_id`).
- `POST /procurement/rfq/{id}/bids` — предложение поставщика (409 если RFQ закрыт).
- `POST /procurement/rfq/{id}/award` — выбрать победителя (`is_winner`, status=awarded, эмит `rfq.awarded`);
  доп. создаёт черновик `PurchaseOrder(status='draft')` у победителя с позицией из RFQ + эмит
  `procurement.po.drafted` (для finance FIN-B1); ответ несёт `created_order_id`. Повтор award → 409.
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
- В `_to_card` балл поставщика — **реальный** (`_board_scores`: по заказам/претензиям/своевременности
  через `supplier_id`), пусто если поставщик не связан или нет данных. Карточка авто-заявки
  (`origin='deficit'`) несёт `status_tag="Авто: дефицит склада"` — фронт рендерит его как бейдж
  (переиспользован существующий `FunnelCard.status_tag`, без правки `core/runtime/funnel.py`).
- `amount` в схемах — `float`, но в модели `Numeric`; в POST приводится `Decimal(str(...))`.
- **`PurchaseOrder` ≠ `PurchaseRequest`:** воронка — пред-заказный sourcing; заказ — уже размещённый,
  с позициями/ETA/статусом. Связи между ними пока нет (плоско). **Оба** эмитят `procurement.received` в
  WMS: воронка на `qc` (одной строкой), заказ на `received` (по каждой позиции) — следить, чтобы один
  физический приход не задвоился (если PR и PO ведут один товар — принимать в одном из путей).
- **Скоркарта**: своевременность считается по `received_at` vs `eta_date` (доля заказов, принятых
  не позже ETA; в знаменателе — только заказы с обоими полями; None — нет таких заказов). Цена —
  honest-empty (нет эталона для нормировки). Балл = взвешенное среднее доступных компонент
  (quality 0.6 / timeliness 0.4). `# ponytail:` веса захардкожены.
- **landed cost — мин.срез:** позиция без `sku_code` или с `qty<=0` пропускается (себестоимость
  единицы не определена — не пишем `unit=0`, чтобы не замаскировать дыру в марже); издержки = только `freight_byn`
  (база — вес, иначе стоимость). Пошлина по ТН ВЭД, два FX-курса и буфер +10% (методика «Расчёт
  Китай») — Горизонт 2. Себестоимость храним в 4 знака, но движок `allocate_landed_cost` отдаёт
  единицу в копейках. **Результат — в СВОЮ таблицу `procurement.landed_cost`, НЕ в
  `integrations.batch.unit_landed_cost`** (это схема СИНК — не трогать).
- `last_landed_cost` → `None` при отсутствии строки (НЕ 0 — иначе спрятали бы дыру в марже).
- **Факт важнее оценки в фасаде:** `last_landed_cost`/`_batch` сортируют `actual`→`estimated` (затем свежее
  по `fixed_at`). Reference-пересчёт (B2) пишет `estimated`-строку (`purchase_order_id=NULL`, `shipment_id=
  "ref-estimate"`) на SKU — она даёт продажам дооприходную себестоимость, но **не затирает** факт приёмки.
  No-op на данных, где только `actual` (вся текущая боевая). Дооприходная оценка ВКЛЮЧАЕТ пошлину, факт
  пока её опускает (Горизонт 2) → после приёмки число может «упасть» до факта; не занижение (оценка ≥ факт
  по пошлине), сведётся, когда пошлина войдёт в `_fixate_landed_cost`.
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