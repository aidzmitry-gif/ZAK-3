"""Read-only primary document projection; procurement retains ownership."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from modules.procurement.receipt_documents import (
    ReceiptDocument,
    ReceiptPosting,
    ReceiptRevision,
    prepare_receipt,
    saved_posting_options,
)


class ProcurementSourceService:
    def __init__(self, services=None):
        self.services = services

    async def landed_financial_source(self, session, organization_id, purchase_order_id, event_id):
        from core.domain.models import OutboxEvent
        from modules.procurement.models import PurchaseOrder
        from modules.procurement.order_edit_commands import (
            PurchaseOrderEditCommand,
            validate_receipt,
        )
        from modules.procurement.ownership import PurchaseOwnership

        if any(type(x) is not int or x <= 0 for x in (organization_id, purchase_order_id, event_id)):
            raise ValueError("Exact organization, purchase order and delivery event are required")
        owner = await session.scalar(select(PurchaseOwnership).where(
            PurchaseOwnership.kind == "order", PurchaseOwnership.source_id == purchase_order_id,
            PurchaseOwnership.organization_id == organization_id,
        ).with_for_update(read=True).execution_options(populate_existing=True))
        order = await session.scalar(select(PurchaseOrder).where(
            PurchaseOrder.id == purchase_order_id,
        ).with_for_update().execution_options(populate_existing=True)) if owner else None
        if order is None or order.status != "received" or order.received_at is None:
            raise ValueError("Owned received purchase-order source is unavailable")
        receipts = (await session.scalars(select(PurchaseOrderEditCommand).where(
            PurchaseOrderEditCommand.ownership_id == owner.id,
            PurchaseOrderEditCommand.target_order_id == order.id,
            PurchaseOrderEditCommand.organization_id == organization_id,
            PurchaseOrderEditCommand.outcome == "applied",
            PurchaseOrderEditCommand.action.in_(("status", "save")),
        ).order_by(PurchaseOrderEditCommand.id).with_for_update(read=True)
            .execution_options(populate_existing=True))).all()
        matches = []
        for receipt in receipts:
            effect = receipt.result.get("effect", {})
            if receipt.action == "save":
                effect = effect.get("status", {})
            if event_id not in effect.get("event_ids", []):
                continue
            await validate_receipt(session, receipt)
            if (effect["from"] == "received" or effect["to"] != "received"
                    or effect["received_at"] != order.received_at.isoformat()):
                raise ValueError("Landed calculation does not match the received purchase order")
            for item in effect.get("landed", []):
                if item["event_id"] == event_id:
                    matches.append((receipt, item["payload"]))
        if len(matches) != 1:
            raise ValueError("Exact landed calculation receipt is unavailable or ambiguous")
        receipt, payload = matches[0]
        event = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id)
            .execution_options(populate_existing=True))
        if (event is None or event.event_type != "procurement.landed_cost.calculated"
                or event.version != 1 or event.payload != payload):
            raise ValueError("Landed event differs from its immutable calculation receipt")
        return {"organization_id": organization_id, "purchase_order_id": order.id,
                "receipt_id": receipt.id, "event_id": event_id, "sku_code": payload["sku_code"],
                "document_date": order.received_at.date().isoformat(), "currency": "BYN",
                "amount": payload["total_landed_byn"], "payload": payload}

    async def additional_expense_source(self, session, organization_id, expense_id, expected_version):
        from modules.procurement.additional_expenses import (
            AdditionalExpenseContent,
            AdditionalExpenseDocument,
            AdditionalExpenseRevision,
            resolve_receipt_lines,
        )
        row = await session.scalar(select(AdditionalExpenseDocument).where(
            AdditionalExpenseDocument.id == expense_id, AdditionalExpenseDocument.organization_id == organization_id))
        if row is None:
            raise ValueError("Additional expense source not found in this organization")
        revision = await session.scalar(select(AdditionalExpenseRevision).where(
            AdditionalExpenseRevision.expense_id == row.id).order_by(AdditionalExpenseRevision.version.desc()).limit(1))
        if revision is None or type(expected_version) is not int or revision.version != expected_version:
            raise ValueError("Additional expense version changed; review the source again")
        document = AdditionalExpenseContent.model_validate(revision.document)
        sources = await resolve_receipt_lines(session, organization_id, document)
        if sources != revision.receipt_sources:
            raise ValueError("Additional expense saved receipt evidence does not match its sources")
        return {"organization_id": organization_id, "expense_id": row.id,
                "source": f"procurement:additional-expense:{row.id}", "version": revision.version,
                "document": document.model_dump(mode="json"), "receipt_sources": sources, "actor": revision.actor}

    async def posted_receipt_basis(self, session, organization_id, receipt_id, expected_version):
        from core.services.procurement import ReceiptAccountingOptions
        source = await self.receipt_source(session, organization_id, receipt_id)
        if source is None or source["status"] != "posted" or type(expected_version) is not int or source["version"] != expected_version:
            raise ValueError("Exact posted receipt source is required")
        posting = await session.get(ReceiptPosting, receipt_id)
        saved_options, frozen_identity = saved_posting_options(posting)
        options = ReceiptAccountingOptions.model_validate(saved_options)
        if options.expected_version != expected_version:
            raise ValueError("Posted receipt options do not match its source version")
        prepared = await prepare_receipt(session, organization_id, receipt_id, options,
                                         frozen_counterparty_id=frozen_identity)
        return {"document": prepared.document,
                "verified_counterparty_id": prepared.verified_counterparty_id,
                "entry_id": posting.entry_id,
                "digest": posting.digest, "actor": posting.actor}

    async def warehouse_receipt_source(self, session, organization_id, receipt_id, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Expected receipt version must be a positive integer")
        row = await session.scalar(select(ReceiptDocument).where(
            ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == organization_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise ValueError("Receipt source not found in this organization")
        source = await self.receipt_source(session, organization_id, receipt_id)
        if row.current_version != expected_version or source["version"] != expected_version:
            raise ValueError("Receipt version changed; review the current primary document")
        if source["correction_of"] is not None:
            raise ValueError("Correction drafts cannot be prepared as warehouse primary receipts")
        if source["status"] not in {"draft", "posted"}:
            raise ValueError("Receipt source is not available for physical receipt")
        from modules.procurement.receipt_documents import ReceiptContent

        document = ReceiptContent.model_validate(source["document"])
        if source["status"] == "draft" and document.supplier_id is None:
            raise ValueError("Match the draft supplier to the catalogue before warehouse receipt")
        if source["status"] == "draft":
            if document.warehouse_id is None or document.warehouse_title is None:
                raise ValueError("Match the draft warehouse to the organization directory before warehouse receipt")
            accounting = getattr(self.services, "accounting", None)
            if accounting is None:
                raise ValueError("Warehouse directory validation service is unavailable")
            try:
                await accounting.verify_warehouse_snapshot(session, organization_id, {
                    "warehouse_id": document.warehouse_id,
                    "warehouse": document.warehouse,
                    "warehouse_title": document.warehouse_title,
                })
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc
        if source["status"] == "draft" and any(
            item.sku_id is None or item.sku_title is None or item.unit is None for item in document.items
        ):
            raise ValueError("Match every draft receipt item to the SKU catalogue before warehouse receipt")
        lines = []
        for position, item in enumerate(document.items, start=1):
            if item.unit is None:
                raise ValueError(f"Receipt line {position}: unit of measure is required")
            if item.quantity >= Decimal("1000000000000") or item.quantity != item.quantity.quantize(Decimal("0.01")):
                raise ValueError(f"Receipt line {position}: quantity cannot be represented by physical receipt storage")
            lines.append({
                "source_line": f"procurement:receipt:{receipt_id}:{expected_version}:{position}",
                "position": position, "sku": item.sku, "lot": item.lot,
                "quantity": str(item.quantity), "unit": item.unit,
            })
        return {"organization_id": organization_id, "receipt_id": receipt_id,
                "version": expected_version, "source": source["source"],
                "document": source["document"], "lines": lines}

    async def confirm_receipt(self, session, organization_id, receipt_id, command, user, accounting, event_bus):
        from modules.procurement.receipt_documents import confirm_receipt

        return await confirm_receipt(session, organization_id, receipt_id, command, user, accounting, event_bus)

    async def prepare_receipt(self, session, organization_id, receipt_id, options, *,
                              require_current_supplier=False):
        return await prepare_receipt(session, organization_id, receipt_id, options,
                                     require_current_supplier=require_current_supplier)

    async def receipt_source(self, session, organization_id, receipt_id):
        row = await session.scalar(select(ReceiptDocument).where(
            ReceiptDocument.id == receipt_id, ReceiptDocument.organization_id == organization_id,
        ))
        if row is None:
            return None
        posted = await session.get(ReceiptPosting, row.id)
        if posted and not 1 <= posted.version <= row.current_version:
            raise ValueError("Receipt posting revision is inconsistent")
        if row.status == "posted" and posted is None:
            raise ValueError("Posted receipt has no posting record")
        correction_of = None
        if row.correction_of_receipt_id is not None:
            correction_of = {"receipt_id": row.correction_of_receipt_id,
                             "version": row.correction_of_version,
                             "entry_id": row.correction_of_entry_id}
        version = posted.version if posted else row.current_version
        revision = await session.scalar(select(ReceiptRevision).where(
            ReceiptRevision.receipt_id == row.id, ReceiptRevision.version == version,
        ))
        if revision is None:
            raise ValueError("Receipt source revision is missing")
        return {
            "id": row.id, "organization_id": row.organization_id,
            "source": f"procurement:receipt:{row.id}", "version": version,
            "status": "posted" if posted else row.status,
            "correction_of": correction_of,
            "entry_id": posted.entry_id if posted else None,
            "document": revision.document, "actor": revision.actor,
            "created_at": revision.created_at,
        }
