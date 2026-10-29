"""Live customer deadlines and terms from explicit outstanding supplier reservations."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from modules.procurement.expected_reservations import ExpectedReservation, ExpectedReservationEvent
from modules.procurement.models import PurchaseOrderLine, PurchaseOrderMilestone
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
    first = await session.scalar(select(PurchaseOrderMilestone).where(
        PurchaseOrderMilestone.order_id == order.id, PurchaseOrderMilestone.stage == "collection"))
    start = None
    if first is not None and first.planned_date is not None:
        try:
            start = first.planned_date - timedelta(days=first.duration_days)
        except OverflowError:
            pass  # A malformed legacy schedule cannot establish a known start.
    start_in_past = start < datetime.now(timezone.utc).date() if start is not None else None
    target = order.target_arrival_date
    items, details = [], []
    for reservation, deal in rows:
        remaining = reservation.qty - consumed.get(reservation.id, Decimal(0))
        if remaining <= 0:
            continue
        deadline = parse_deadline(deal.ship_deadline)
        try:
            arrival = arrival_deadline(deadline)
        except OverflowError:
            arrival = None
        deadline_status = "dated" if arrival else ("unrepresentable_arrival" if deadline else "missing_or_unparsed")
        slack = (arrival - target).days if arrival is not None and target is not None else None
        items.append({"reservation_id": reservation.id, "deal_id": deal.id,
            "order_line_id": reservation.order_line_id, "sku_code": reservation.sku_code,
            "outstanding_qty": f"{remaining:.2f}", "ship_deadline": deal.ship_deadline,
            "required_arrival": arrival.isoformat() if arrival else None,
            "deadline_status": deadline_status,
            "at_risk": (target is None or target > arrival) if arrival else None})
        details.append({"reservation_id": reservation.id, "number": deal.number,
            "counterparty": deal.counterparty, "required_by": deadline.isoformat() if arrival else None,
            "slack_days": slack,
            "penalty_rate_pct": f"{deal.penalty_rate_pct:.2f}" if deal.penalty_rate_pct is not None else None,
            "penalty_cap_pct": f"{deal.penalty_cap_pct:.2f}" if deal.penalty_cap_pct is not None else None,
            "penalty_terms": deal.penalty_terms})
    dated = [x["required_arrival"] for x in items if x["required_arrival"]]
    required_by = [x["required_by"] for x in details if x["required_by"]]
    arrival = min(dated) if dated else None
    unresolved = sum(x["deadline_status"] != "dated" for x in items)
    return {"organization_id": org_id, "order_id": order.id, "status": "live_review",
        "source": "outstanding_expected_reservations", "items": items,
        "earliest_required_arrival": arrival,
        "unresolved_deadlines": unresolved, "complete_customer_demand": False,
        "at_risk": True if start_in_past or any(x["at_risk"] is True for x in items)
                   else (None if unresolved or not items else False),
        "risk_details": {"version": 1, "required_by": min(required_by) if required_by else None,
            "target_arrival_date": target.isoformat() if target is not None else None,
            "slack_days": (datetime.fromisoformat(arrival).date() - target).days if arrival and target else None,
            "schedule_start_date": start.isoformat() if start is not None else None,
            "schedule_start_in_past": start_in_past, "items": details}}
