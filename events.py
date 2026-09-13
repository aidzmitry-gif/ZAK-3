"""Обработчики событий модуля Procurement."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

logger = logging.getLogger("aios.procurement")


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def on_production_scrap(payload: dict, ctx) -> None:
    """Брак в производстве (ОТК) → автопретензия поставщику (production → procurement).

    Замыкает цикл качества: дефект, найденный на сборке (``production.scrap``),
    фиксируется претензией в закупках. Поставщик в событии не приходит — открываем
    претензию без поставщика (``status="open"``); закупщик проставит поставщика и
    закроет её через ``PATCH /procurement/claims``. Обработчик с контекстом: пишет
    в сессию relay, коммит делает relay (не здесь) — §2.5.
    """
    if ctx is None:
        return
    from modules.procurement.models import SupplierClaim

    ctx.session.add(
        SupplierClaim(
            item=payload.get("item", ""),
            reason=payload.get("reason", ""),
            order_code=payload.get("order_code", ""),
            entity_ref=payload.get("entity_ref", ""),
            status="open",
            source="production",
        )
    )
    logger.info(
        "Procurement: претензия по браку — %s (наряд %s)",
        payload.get("item"),
        payload.get("order_code"),
    )


async def on_stock_low(payload: dict, ctx) -> None:
    """Сигнал дефицита склада → авто-черновик заявки на закупку (wms → procurement, MRP-lite).

    Канонический ПЛОСКИЙ payload (одно событие на нарушенный порог): ``{sku_code, sku_title,
    warehouse, free_qty, min_qty, deficit, reorder_qty, severity, source, entity_ref}``. Создание
    и идемпотентность держит ``_request_from_deficit`` (переиспользуем, не дублируем). Обработчик
    с ``(payload, ctx)``: пишет в сессию relay, коммит делает relay (не здесь) — §2.5.
    """
    if ctx is None:
        return
    sku_code = payload.get("sku_code")
    if not sku_code:
        logger.warning("Procurement: wms.stock.low без sku_code — пропуск (%s)", payload)
        return
    from modules.procurement.routes import _request_from_deficit

    await _request_from_deficit(
        ctx.session,
        sku_code=sku_code,
        sku_title=payload.get("sku_title") or sku_code,  # фриз: item := sku_title (fallback sku_code)
        warehouse=payload.get("warehouse") or "Главный",
        deficit=payload.get("deficit") or 0,
        reorder_qty=payload.get("reorder_qty") or 0,
    )
    logger.info(
        "Procurement: автозаявка по дефициту — %s (%s, дефицит %s)",
        payload.get("sku_title") or sku_code,
        payload.get("warehouse"),
        payload.get("deficit"),
    )


async def on_ship_deadline_set(payload: dict, ctx) -> None:
    """Срок отгрузки клиенту из продаж → требования закупок по (сделка, sku) (sales → procurement).

    Закупки планируют машину к самому раннему сроку позиций − буфер «последней мили», иначе риск
    срыва отгрузки и штрафа. Идемпотентно: upsert по (deal_id, sku_code); повторный сигнал
    обновляет срок/штраф; позиции, убранные из сделки, снимаются (только если items не пуст —
    защита от случайного стирания). Обработчик с ``(payload, ctx)``: коммит делает relay, не здесь.
    """
    if ctx is None:
        return
    from sqlalchemy import select

    from modules.procurement.models import ShipRequirement
    from modules.procurement.plan import parse_deadline

    deal_id = payload.get("deal_id")
    if deal_id is None:
        logger.warning("Procurement: ship_deadline.set без deal_id — пропуск (%s)", payload)
        return
    raw = payload.get("ship_deadline")
    parsed = parse_deadline(raw)
    items = payload.get("items") or []
    existing = {
        r.sku_code: r
        for r in (await ctx.session.execute(
            select(ShipRequirement).where(ShipRequirement.deal_id == deal_id)
        )).scalars().all()
    }
    seen: set[str] = set()
    for it in items:
        sku = (it.get("sku_code") or "").strip()
        if not sku:
            continue
        seen.add(sku)
        fields = dict(
            number=payload.get("number") or "",
            counterparty=payload.get("counterparty") or "",
            qty=Decimal(str(it.get("qty") or 0)),
            ship_deadline=raw,
            ship_deadline_date=parsed,
            penalty_rate_pct=_dec(payload.get("penalty_rate_pct")),
            penalty_cap_pct=_dec(payload.get("penalty_cap_pct")),
            penalty_terms=payload.get("penalty_terms"),
        )
        row = existing.get(sku)
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
        else:
            row = ShipRequirement(deal_id=deal_id, sku_code=sku, **fields)
            ctx.session.add(row)
            existing[sku] = row  # дубль того же sku в items → обновим эту строку (UNIQUE deal_id+sku_code)
    if items:  # позиции, убранные из сделки → снять требование (но не стираем на пустом сигнале)
        for sku, row in existing.items():
            if sku not in seen:
                await ctx.session.delete(row)
    logger.info(
        "Procurement: срок клиента по сделке %s → %s (%d поз.)",
        payload.get("number"), raw, len(seen),
    )


async def _affected_skus(session, ref_key: str, key: str) -> list[str]:
    """Коды SKU, затронутые сменой справочника: ``core.skus`` → сам товар; ``core.tnved`` → товары
    с этим (своим) кодом ТН ВЭД. Групповое наследование ТН ВЭД — отложенный каскад (# ponytail:
    резолв через группы номенклатуры, если понадобится точность по наследованию)."""
    from sqlalchemy import select

    from core.domain.models import Sku

    if ref_key == "core.skus":
        return [key]
    if ref_key == "core.tnved":
        return list(
            (await session.execute(select(Sku.code).where(Sku.tnved_code == key))).scalars().all()
        )
    return []


async def on_reference_changed(payload: dict, ctx) -> None:
    """Смена справочной ставки/мастер-поля SKU → пересчёт плановой landed затронутых товаров
    (reference → procurement, REF3-7 / круг 4 B2).

    Шина без wildcard, поэтому подписка на КОНКРЕТНЫЕ события: ``reference.ref_tnved.changed``
    (пошлина) и ``reference.sku.changed`` (мастер-поля товара). НДС/курс — доля Финансов (НДС
    возвратный — не входит в landed; товар заказа уже в BYN — курс не двигает BYN-landed).

    ⚠️ Дебаунс против каскада: массовая правка справочника = шквал событий. В пределах одного
    прохода relay каждый SKU пересчитываем не более раза — кэш на ``ctx`` (relay переиспользует
    один ``EventContext`` на весь батч). Коммит делает relay, не обработчик (§2.5).
    """
    if ctx is None:
        return
    ref_key = payload.get("ref_key") or ""
    entity_ref = payload.get("entity_ref") or ""
    key = entity_ref.split(":", 1)[1] if ":" in entity_ref else ""
    if not key:
        return
    codes = await _affected_skus(ctx.session, ref_key, key)
    if not codes:
        return
    from modules.procurement.routes import _recompute_estimated_landed

    done = getattr(ctx, "_procurement_ref_recomputed", None)
    if done is None:
        done = set()
        ctx._procurement_ref_recomputed = done  # дедуп пересчётов за один проход relay
    fresh = [c for c in codes if c not in done]
    for code in fresh:
        done.add(code)
        await _recompute_estimated_landed(ctx.session, code)
    logger.info(
        "Procurement: reference %s → пересчёт плановой landed (%d из %d, остальное — дедуп)",
        ref_key, len(fresh), len(codes),
    )


async def on_deal_loss_finalized(payload: dict, ctx) -> None:
    """Проигрыш сделки освобождает её незакрытую часть ожидаемых резервов.

    Резервная запись и release-событие остаются в истории. Повторная доставка
    одного resolution UUID находит тот же ключ события и не меняет остаток второй раз.
    """
    if ctx is None:
        return
    organization_id = payload.get("organization_id")
    deal_id = payload.get("deal_id")
    resolution_id = payload.get("resolution_id")
    if type(organization_id) is not int or organization_id <= 0 or type(deal_id) is not int or deal_id <= 0 or not resolution_id:
        logger.warning("Procurement: неполное событие потери сделки — %s", payload)
        return
    from sqlalchemy import func, select

    from modules.accounting.models import Organization
    from modules.procurement.expected_reservations import (
        ExpectedReservation,
        ExpectedReservationEvent,
    )

    await ctx.session.scalar(select(Organization).where(Organization.id == organization_id).with_for_update())
    rows = (
        await ctx.session.scalars(
            select(ExpectedReservation)
            .where(
                ExpectedReservation.organization_id == organization_id,
                ExpectedReservation.deal_id == deal_id,
            )
            .with_for_update()
        )
    ).all()
    released_count = 0
    for row in rows:
        request_key = str(uuid5(NAMESPACE_URL, f"crm-erp:deal-loss:{resolution_id}:{row.id}"))
        existing = await ctx.session.scalar(
            select(ExpectedReservationEvent)
            .where(
                ExpectedReservationEvent.organization_id == organization_id,
                ExpectedReservationEvent.request_key == request_key,
            )
            .with_for_update()
        )
        if existing is not None:
            continue
        totals = (
            await ctx.session.execute(
                select(ExpectedReservationEvent.kind, func.coalesce(func.sum(ExpectedReservationEvent.qty), 0))
                .where(
                    ExpectedReservationEvent.organization_id == organization_id,
                    ExpectedReservationEvent.reservation_id == row.id,
                )
                .group_by(ExpectedReservationEvent.kind)
            )
        ).all()
        by_kind = {kind: Decimal(total or 0) for kind, total in totals}
        remaining = max(row.qty - by_kind.get("release", Decimal("0")) - by_kind.get("convert", Decimal("0")), Decimal("0"))
        if remaining <= 0:
            continue
        ctx.session.add(
            ExpectedReservationEvent(
                organization_id=organization_id,
                reservation_id=row.id,
                kind="release",
                qty=remaining,
                request_key=request_key,
                evidence=f"Deal {deal_id} lost; resolution {resolution_id}",
                actor=str(payload.get("by") or payload.get("actor") or "system"),
            )
        )
        released_count += 1
    await ctx.session.flush()
    await _reconcile_expected_requests(ctx, organization_id)
    logger.info(
        "Procurement: потеря сделки %s освободила %d ожидаемых резервов",
        deal_id,
        released_count,
    )


def _expected_stock_quantities(payload: dict) -> dict[str, Decimal]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Expected conversion requires nonempty stock lines")
    quantities: dict[str, Decimal] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("sku_code"), str):
            raise ValueError("Expected conversion stock line is invalid")
        sku = item["sku_code"].strip()
        if not sku or len(sku) > 64:
            raise ValueError("Expected conversion SKU is invalid")
        try:
            qty = Decimal(str(item.get("qty")))
            if (not qty.is_finite() or qty <= 0 or qty >= Decimal("1000000000000")
                    or qty != qty.quantize(Decimal("0.01"))):
                raise ValueError("Expected conversion quantity is invalid")
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Expected conversion quantity is invalid") from None
        quantities[sku] = quantities.get(sku, Decimal("0")) + qty
        if quantities[sku] >= Decimal("1000000000000"):
            raise ValueError("Expected conversion SKU quantity exceeds storage capacity")
    return quantities


async def on_stock_reserved_for_expected(payload: dict, ctx) -> None:
    """Snapshot the reserve event before QC, without blocking later outbox rows."""
    if ctx is None:
        return
    from sqlalchemy import select

    from modules.accounting.models import Organization
    from modules.procurement.expected_reservations import (
        ExpectedConversionRequest,
        ExpectedReservation,
    )

    org = payload.get("organization_id")
    document_id = payload.get("document_id")
    if type(org) is not int or org <= 0 or type(document_id) is not int or document_id <= 0:
        return
    # Reject malformed data before persisting a pending request. Pending means
    # valid stock evidence awaiting QC, not a silently swallowed input error.
    _expected_stock_quantities(payload)
    # All request creation/reconciliation for an owner takes this same lock.
    if await ctx.session.scalar(select(Organization).where(Organization.id == org).with_for_update()) is None:
        raise ValueError("Expected conversion organization is unavailable")
    identity = str(ctx.event_id)
    if type(ctx.event_id) is not int or ctx.event_id <= 0:
        raise ValueError("Expected conversion requires a durable event identity")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    reservation_digest = payload.get("reservation_digest")
    reservation_identity = None
    if reservation_digest is not None:
        if not isinstance(reservation_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", reservation_digest):
            raise ValueError("Expected conversion reservation digest is invalid")
        reservation_identity = hashlib.sha256(
            f"{org}:{document_id}:{reservation_digest}".encode(),
        ).hexdigest()
    request = await ctx.session.scalar(select(ExpectedConversionRequest).where(
        ExpectedConversionRequest.organization_id == org,
        ExpectedConversionRequest.event_identity == identity,
    ).with_for_update())
    if request is None and reservation_identity is not None:
        request = await ctx.session.scalar(select(ExpectedConversionRequest).where(
            ExpectedConversionRequest.organization_id == org,
            ExpectedConversionRequest.reservation_identity == reservation_identity,
        ).with_for_update())
    if request is None:
        ids = list((await ctx.session.scalars(select(ExpectedReservation.id).where(
            ExpectedReservation.organization_id == org, ExpectedReservation.document_id == document_id,
        ).order_by(ExpectedReservation.id))).all())
        request = ExpectedConversionRequest(organization_id=org, event_identity=identity,
            reservation_identity=reservation_identity, payload_hash=digest,
            payload=payload, reservation_ids=ids, completed=False)
        ctx.session.add(request)
        await ctx.session.flush()
    elif request.payload_hash != digest:
        raise ValueError("Expected conversion event payload changed")
    await _reconcile_expected_requests(ctx, org)


async def _reconcile_expected_requests(ctx, organization_id):
    from sqlalchemy import select

    from modules.procurement.expected_reservations import ExpectedConversionRequest

    requests = (await ctx.session.scalars(select(ExpectedConversionRequest).where(
        ExpectedConversionRequest.organization_id == organization_id,
        ExpectedConversionRequest.completed.is_(False),
    ).order_by(ExpectedConversionRequest.id).with_for_update())).all()
    for request in requests:
        if await _convert_expected_request(request.payload, ctx, request):
            request.completed = True
    await ctx.session.flush()


async def _convert_expected_request(payload: dict, ctx, request) -> bool:
    """Close the expected-reserve leg after a verified WMS invoice reserve.

    ``sales.stock.reserved`` is delivered through the same transactional outbox
    relay as the WMS reservation itself.  The handler therefore appends only the
    accepted, FIFO-eligible part of the snapshotted expected reservations; it
    never creates warehouse movements. Missing QC leaves the durable request
    pending without partial conversion. A deterministic key per event
    and expected-reservation row makes redelivery harmless.
    """
    if ctx is None:
        return
    organization_id = payload.get("organization_id")
    document_id = payload.get("document_id")
    if (type(organization_id) is not int or organization_id <= 0
            or type(document_id) is not int or document_id <= 0):
        return
    quantities = _expected_stock_quantities(payload)

    from sqlalchemy import select

    from modules.procurement.expected_reservations import (
        ExpectedReservation,
        ExpectedReservationEvent,
        _projection,
    )
    from modules.procurement.models import PurchaseOrderLine

    rows = (await ctx.session.scalars(
        select(ExpectedReservation)
        .where(
            ExpectedReservation.organization_id == organization_id,
            ExpectedReservation.document_id == document_id,
        )
        .order_by(ExpectedReservation.id)
        .with_for_update()
    )).all()
    rows = [row for row in rows if row.id in request.reservation_ids]
    if not rows:
        return True

    event_identity = int(request.event_identity)
    actor = payload.get("by") or payload.get("actor") or "wms"
    if not isinstance(actor, str) or not actor:
        actor = "wms"
    digest = payload.get("reservation_digest")
    evidence = f"WMS sales.stock.reserved {event_identity}"
    if isinstance(digest, str) and digest:
        evidence += f" digest={digest[:64]}"

    planned = []
    for row in rows:
        amount = quantities.get(row.sku_code, Decimal("0"))
        if amount <= 0:
            continue
        request_key = str(uuid5(
            NAMESPACE_URL,
            f"crm-erp:expected-conversion:{event_identity}:{row.id}",
        ))
        existing = await ctx.session.scalar(select(ExpectedReservationEvent).where(
            ExpectedReservationEvent.organization_id == organization_id,
            ExpectedReservationEvent.request_key == request_key,
        ).with_for_update())
        if existing is not None:
            if (existing.reservation_id != row.id or existing.kind != "convert"
                    or existing.organization_id != organization_id):
                raise ValueError("Expected conversion key identifies another event")
            quantities[row.sku_code] = max(amount - existing.qty, Decimal("0"))
            continue
        line = await ctx.session.scalar(select(PurchaseOrderLine).where(
            PurchaseOrderLine.id == row.order_line_id,
        ).with_for_update())
        if line is None or line.sku_code != row.sku_code:
            raise ValueError("Expected conversion order line is inconsistent")
        projection = await _projection(ctx.session, organization_id, line)
        current = next(item for item in projection["reservations"] if item["id"] == row.id)
        remaining = Decimal(current["qty"]) - Decimal(current["released"]) - Decimal(current["converted"])
        take = min(amount, max(remaining, Decimal("0")))
        if take > Decimal(current["physical_convertible"]):
            return False
        if take <= 0:
            continue
        planned.append(ExpectedReservationEvent(
            organization_id=organization_id,
            reservation_id=row.id,
            kind="convert",
            qty=take,
            request_key=request_key,
            evidence=evidence,
            actor=actor,
        ))
        quantities[row.sku_code] = amount - take
    ctx.session.add_all(planned)
    await ctx.session.flush()
    return True


async def on_physical_receipt_accepted(payload: dict, ctx) -> None:
    """Persist exact QC quantities for a WMS receipt bound to procurement.

    WMS owns the physical receipt and emits this event only after its accepted
    movement is committed to the relay transaction.  Procurement stores the
    source-line mapping as immutable evidence; later expected-reserve
    conversions use this register instead of treating a saved primary draft as
    physically accepted.
    """
    if ctx is None:
        return
    event_id = getattr(ctx, "event_id", None)
    organization_id = payload.get("organization_id")
    receipt_id = payload.get("receipt_id")
    source_receipt_id = payload.get("source_receipt_id")
    source_version = payload.get("source_version")
    lines = payload.get("lines")
    if (type(event_id) is not int or event_id <= 0
            or type(organization_id) is not int or organization_id <= 0
            or type(receipt_id) is not int or receipt_id <= 0
            or type(source_receipt_id) is not int or source_receipt_id <= 0
            or type(source_version) is not int or source_version <= 0
            or not isinstance(lines, list) or not lines):
        raise ValueError("Physical receipt acceptance identity is incomplete")
    from sqlalchemy import select

    from modules.procurement.receipt_documents import ReceiptDocument, ReceiptRevision

    revision = await ctx.session.scalar(select(ReceiptRevision).join(
        ReceiptDocument, ReceiptDocument.id == ReceiptRevision.receipt_id,
    ).where(
        ReceiptDocument.organization_id == organization_id,
        ReceiptRevision.receipt_id == source_receipt_id,
        ReceiptRevision.version == source_version,
    ))
    if revision is None:
        raise ValueError("Physical receipt acceptance source version is unavailable")
    from modules.accounting.models import Organization

    await ctx.session.scalar(select(Organization).where(Organization.id == organization_id).with_for_update())
    facts = revision.document
    source_items = facts.get("items") if isinstance(facts, dict) else None
    if not isinstance(source_items, list) or not source_items:
        raise ValueError("Physical receipt acceptance source lines are unavailable")
    normalized = []
    seen_positions = set()
    for item in lines:
        if not isinstance(item, dict) or type(item.get("position")) is not int:
            raise ValueError("Physical receipt acceptance line is invalid")
        position = item["position"]
        if position <= 0 or position > len(source_items) or position in seen_positions:
            raise ValueError("Physical receipt acceptance position is invalid")
        seen_positions.add(position)
        fact = source_items[position - 1]
        order_line_id = fact.get("order_line_id") if isinstance(fact, dict) else None
        sku_code = fact.get("sku") if isinstance(fact, dict) else None
        # Primary receipts may contain goods purchased without a supplier order.
        # Such lines have no expected-reserve leg to reconcile.
        if order_line_id is None and isinstance(fact, dict):
            continue
        if type(order_line_id) is not int or order_line_id <= 0 or not isinstance(sku_code, str) or not sku_code:
            raise ValueError("Physical receipt acceptance requires an exact procurement order line")
        if item.get("sku_code") != sku_code:
            raise ValueError("Physical receipt acceptance SKU differs from the primary source")
        try:
            accepted = Decimal(str(item.get("accepted_qty")))
            source_qty = Decimal(str(fact.get("quantity")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Physical receipt acceptance quantity is invalid") from None
        if (not accepted.is_finite() or accepted < 0 or accepted != accepted.quantize(Decimal("0.01"))
                or not source_qty.is_finite() or accepted > source_qty):
            raise ValueError("Physical receipt acceptance quantity exceeds the primary line")
        source_line = item.get("source_line")
        if source_line != f"procurement:receipt:{source_receipt_id}:{source_version}:{position}":
            raise ValueError("Physical receipt acceptance source line differs from the primary source")
        normalized.append({
            "position": position,
            "order_line_id": order_line_id,
            "sku_code": sku_code,
            "accepted_qty": format(accepted, ".2f"),
            "source_line": source_line,
        })
    normalized.sort(key=lambda row: row["position"])
    if not normalized:
        return
    from sqlalchemy import select

    from modules.procurement.expected_reservations import PhysicalReceiptAcceptance

    existing = await ctx.session.scalar(select(PhysicalReceiptAcceptance).where(
        PhysicalReceiptAcceptance.organization_id == organization_id,
        PhysicalReceiptAcceptance.event_id == event_id,
    ).with_for_update())
    if existing is not None:
        if (existing.receipt_id != receipt_id or existing.source_receipt_id != source_receipt_id
                or existing.source_version != source_version or existing.lines != normalized):
            raise ValueError("Physical receipt acceptance event identifies another source")
        return
    existing_receipt = await ctx.session.scalar(select(PhysicalReceiptAcceptance).where(
        PhysicalReceiptAcceptance.organization_id == organization_id,
        PhysicalReceiptAcceptance.receipt_id == receipt_id,
    ).with_for_update())
    if existing_receipt is not None:
        if (existing_receipt.source_receipt_id != source_receipt_id
                or existing_receipt.source_version != source_version
                or existing_receipt.lines != normalized):
            raise ValueError("Physical receipt already has another acceptance snapshot")
        return
    actor = payload.get("by") or payload.get("actor") or "wms"
    if not isinstance(actor, str) or not actor:
        actor = "wms"
    ctx.session.add(PhysicalReceiptAcceptance(
        organization_id=organization_id,
        event_id=event_id,
        receipt_id=receipt_id,
        source_receipt_id=source_receipt_id,
        source_version=source_version,
        lines=normalized,
        evidence=str(payload.get("evidence") or f"WMS receipt {receipt_id} accepted"),
        actor=actor,
    ))
    await ctx.session.flush()
    await _reconcile_expected_requests(ctx, organization_id)
