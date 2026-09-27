"""Current MDM identity gate for new procurement source documents.

Historical snapshots remain readable even after a counterparty is renamed or
merged; only a new business write depends on the present catalog state.
"""

from sqlalchemy import func, select

from core.domain.models import Counterparty
from modules.procurement.models import Supplier


def active_bound_suppliers():
    """Profiles whose immutable ID and displayed requisites still match MDM."""
    return (select(Supplier).join(Counterparty, Supplier.counterparty_id == Counterparty.id)
            .where(Supplier.status == "active", Counterparty.is_active.is_(True),
                   Counterparty.merged_into_id.is_(None),
                   Supplier.name == func.coalesce(func.nullif(Counterparty.legal_name, ""), Counterparty.name),
                   Supplier.unp == func.coalesce(Counterparty.unp, "")))


async def selected_supplier(session, supplier_id: int, name: str, unp: str):
    """Lock the MDM and supplier facts used by a new document until commit."""
    return await session.scalar(active_bound_suppliers().where(
        Supplier.id == supplier_id, Supplier.name == name, Supplier.unp == unp,
    ).with_for_update(read=True))
