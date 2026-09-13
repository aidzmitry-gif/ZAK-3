"""Exact expected-stock projection. Callers must supply verified, scoped source facts.

No warehouse availability or reservation is created by this calculation.
"""
from decimal import Decimal


def quantity(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Expected an exact quantity string")
    import re
    if not re.fullmatch(r"(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?", value):
        raise ValueError("Invalid expected-stock quantity")
    return Decimal(value)


def project_line(*, ordered: str, accepted: str, cancelled: str, allocations: list[dict]) -> dict:
    """Allocation quantities are lifetime totals, with explicit release/conversion.

    Rejected goods still owed by the supplier stay expected. A supplier reduction
    is represented explicitly by cancelled, not inferred from receipt status.
    """
    total, arrived, removed = map(quantity, (ordered, accepted, cancelled))
    if arrived + removed > total:
        raise ValueError("Accepted and cancelled exceed ordered quantity")
    pending = Decimal(0)
    converted = Decimal(0)
    clients = []
    seen = set()
    for row in allocations:
        identity = row.get("allocation_id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("Allocation identity is missing or repeated")
        seen.add(identity)
        allocated, released, physical = map(quantity, (row["allocated"], row["released"], row["converted"]))
        if released + physical > allocated:
            raise ValueError("Released and converted exceed client allocation")
        remaining = allocated - released - physical
        pending += remaining
        converted += physical
        clients.append({"allocation_id": identity, "expected_reserved": f"{remaining:.2f}",
                        "converted": f"{physical:.2f}", "released": f"{released:.2f}"})
    if converted > arrived:
        raise ValueError("Physical conversion exceeds accepted quantity")
    expected = total - arrived - removed
    free = expected - pending
    # Preserve the conflict: reducing a PO must not silently erase client claims.
    return {"ordered": f"{total:.2f}", "accepted": f"{arrived:.2f}", "cancelled": f"{removed:.2f}",
            "expected": f"{expected:.2f}", "expected_reserved": f"{pending:.2f}",
            "free_expected": f"{max(free, Decimal(0)):.2f}",
            "uncovered": f"{max(-free, Decimal(0)):.2f}",
            "allocation_allowed": free > 0, "clients": clients}
