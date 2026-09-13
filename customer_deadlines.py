"""Live customer deadlines from explicit outstanding supplier reservations."""
from decimal import Decimal

from sqlalchemy import func, select

from modules.procurement.expected_reservations import ExpectedReservation, ExpectedReservationEvent
from modules.procurement.models import PurchaseOrderLine
from modules.procurement.plan import arrival_deadline, parse_deadline
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.models import Deal


async def review(session, org_id, order):
    rows = (await session.execute(select(ExpectedReservation, Deal)
        .join(Deal, Deal.id == ExpectedReservation.deal_id)
        .join(DealOwnership, (DealOwnership.deal_id == Deal.id) & (DealOwnership.organization_id == org_id))
        .join(PurchaseOrderLine, (PurchaseOrderLine.id == ExpectedReservation.order_line_id)
              & (PurchaseOrderLine.order_id == order.id)
              & (PurchaseOrderLine.sku_code == ExpectedReservation.sku_code))
        .where(ExpectedReservation.organization_id == org_id,
               ExpectedReservation.order_id == order.id)
        .order_by(ExpectedReservation.id))).all()
    ids = [r.id for r, _ in rows]
    consumed = dict((await session.execute(select(ExpectedReservationEvent.reservation_id,
        func.sum(ExpectedReservationEvent.qty)).where(
            ExpectedReservationEvent.organization_id == org_id,
            ExpectedReservationEvent.reservation_id.in_(ids)
        ).group_by(ExpectedReservationEvent.reservation_id))).all()) if ids else {}
    items = []
    for reservation, deal in rows:
        remaining = reservation.qty - consumed.get(reservation.id, Decimal(0))
        if remaining <= 0:
            continue
        deadline = parse_deadline(deal.ship_deadline)
        arrival = arrival_deadline(deadline)
        target = order.target_arrival_date
        items.append({"reservation_id": reservation.id, "deal_id": deal.id,
            "order_line_id": reservation.order_line_id, "sku_code": reservation.sku_code,
            "outstanding_qty": f"{remaining:.2f}", "ship_deadline": deal.ship_deadline,
            "required_arrival": arrival.isoformat() if arrival else None,
            "deadline_status": "dated" if deadline else "missing_or_unparsed",
            "at_risk": (target is None or target > arrival) if arrival else None})
    dated = [x["required_arrival"] for x in items if x["required_arrival"]]
    unresolved = sum(x["deadline_status"] != "dated" for x in items)
    return {"organization_id": org_id, "order_id": order.id, "status": "live_review",
        "source": "outstanding_expected_reservations", "items": items,
        "earliest_required_arrival": min(dated) if dated else None,
        "unresolved_deadlines": unresolved, "complete_customer_demand": False,
        "at_risk": True if any(x["at_risk"] is True for x in items)
                   else (None if unresolved or not items else False)}
