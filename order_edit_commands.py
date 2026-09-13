"""Durable editor receipts. Organization lock serializes execute against reconcile."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import OutboxEvent
from core.runtime.deps import get_core
from modules.procurement import routes
from modules.procurement.models import PurchaseOrder, PurchaseOrderLine, TransportMethod
from modules.procurement.order_creation import line_result
from modules.procurement.ownership import (
    PurchaseOwnership,
    effective_plan_user,
    plan_context,
    request_command_hash,
)
from modules.procurement.plan import DEFAULT_METHODS, STAGE_ORDER, build_milestone_plan
from modules.procurement.receipt_documents import immutable
from modules.procurement.schemas import (
    EditorDeleteInput,
    EditorHeaderInput,
    EditorLineInput,
    EditorPlanInput,
    EditorStatusInput,
    ScopedOrderPlanOut,
)

Action = Literal["add_line", "delete_line", "header", "status", "plan"]
INPUTS = {"add_line": EditorLineInput, "delete_line": EditorDeleteInput, "header": EditorHeaderInput, "status": EditorStatusInput, "plan": EditorPlanInput}
CODES = {"command_abandoned", "source_unavailable", "order_not_editable", "line_unavailable", "transition_not_allowed", "transport_method_unavailable"}


class EditCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    request_key: str = Field(strict=True)
    order_id: int = Field(strict=True, gt=0, le=2147483647)
    action: Action
    payload: dict

    @field_validator("version", mode="before")
    @classmethod
    def protocol_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Protocol version 1 required")
        return value

    @field_validator("request_key")
    @classmethod
    def key(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Canonical UUID required")
        return value

    @model_validator(mode="after")
    def canonical(self):
        parsed = INPUTS[self.action].model_validate(self.payload)
        if self.action == "header" and not parsed.model_fields_set:
            raise ValueError("At least one header field is required")
        data = parsed.model_dump(mode="json", exclude_unset=self.action == "header")
        for name, scale in {"qty": 2, "goods_value_byn": 2, "weight": 3, "volume": 4, "freight_byn": 2}.items():
            if name in data:
                data[name] = format(Decimal(data[name]), f".{scale}f")
        self.payload = data
        return self


class PurchaseOrderEditCommand(Base):
    __tablename__ = "purchase_order_edit_command"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key"),
        CheckConstraint("organization_id > 0 AND target_order_id > 0", name="edit_positive_ids"),
        CheckConstraint("action IN ('add_line','delete_line','header','status','plan')", name="edit_action"),
        CheckConstraint("(outcome='applied' AND ownership_id IS NOT NULL) OR (outcome='rejected' AND ownership_id IS NULL)", name="edit_outcome"),
        {"schema": "procurement"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id"))
    request_key: Mapped[str] = mapped_column(String(36))
    actor: Mapped[str] = mapped_column(String(200))
    target_order_id: Mapped[int] = mapped_column(Integer)  # tombstones can name missing sources
    action: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16))
    ownership_id: Mapped[int | None] = mapped_column(ForeignKey("procurement.purchase_ownership.id"))
    command: Mapped[dict] = mapped_column(JSON)
    command_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(PurchaseOrderEditCommand, "before_update", immutable)
event.listen(PurchaseOrderEditCommand, "before_delete", immutable)
router = APIRouter(tags=["Durable order editing"])


async def writer(org_id: int, x_expected_organization: int = Header(gt=0),
                 x_expected_principal: str = Header(min_length=1, max_length=200), ctx=Depends(plan_context)):
    session, gateway, user = ctx
    await gateway.source_owner_authority(session, org_id, user)
    user = await effective_plan_user(session, user)
    actor = await gateway.source_owner_authority(session, org_id, user)
    if x_expected_organization != org_id or x_expected_principal != actor:
        raise HTTPException(409, "Expected command context differs")
    return session, actor


def common(org, actor, command):
    return {"version": 1, "organization_id": org, "principal": actor,
        "request_key": command.request_key, "command_hash": request_command_hash(command.model_dump(mode="json")),
        "order_id": command.order_id, "action": command.action}


def exact(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def valid_line(value):
    if not exact(value, ["id", "sku_code", "qty", "goods_value_byn", "weight", "volume"]):
        return False
    if type(value["id"]) is not int or value["id"] <= 0 or not isinstance(value["sku_code"], str):
        return False
    # Historical legacy rows can contain zero quantity/empty SKU. Do not revalidate
    # them as a new input or require the live row to survive deletion.
    try:
        return all(isinstance(value[x], str) and Decimal(value[x]).is_finite() for x in ["qty", "goods_value_byn", "weight", "volume"])
    except Exception:
        return False


def valid_effect(action, payload, effect, org, order_id, actor):
    if action in {"add_line", "delete_line"}:
        if not exact(effect, ["line"]) or not valid_line(effect["line"]):
            return False
        line = effect["line"]
        return ({k: v for k, v in line.items() if k != "id"} == payload if action == "add_line" else line["id"] == payload["line_id"])
    if action == "header":
        return (exact(effect, ["before", "after"]) and exact(effect["before"], payload) and effect["after"] == payload
            and all((isinstance(v, str) if k in {"supplier", "freight_byn"} else v is None or type(v) is int if k == "supplier_id" else v is None or isinstance(v, str)) for k, v in effect["before"].items()))
    if action == "status":
        return (exact(effect, ["from", "to", "received_at", "event_ids"]) and effect["from"] in {"draft", "ordered", "shipped", "customs", "received", "cancelled"}
            and effect["to"] == payload["status"] and (effect["received_at"] is None or isinstance(effect["received_at"], str))
            and isinstance(effect["event_ids"], list) and all(type(x) is int and x > 0 for x in effect["event_ids"])
            and len(set(effect["event_ids"])) == len(effect["event_ids"])
            and bool(effect["event_ids"]) == (effect["from"] != effect["to"]))
    if action == "plan":
        try:
            parsed = ScopedOrderPlanOut.model_validate(effect).model_dump(mode="json")
            ms = parsed["milestones"]
            if len(ms) != len(STAGE_ORDER) or [m["stage"] for m in ms] != STAGE_ORDER or any(m["seq"] != i or m["duration_days"] < 0 for i, m in enumerate(ms)):
                return False
            planned, start = build_milestone_plan({m["stage"]: m["duration_days"] for m in ms}, date.fromisoformat(payload["target_arrival_date"]))
            consistent = parsed["start_date"] == start.isoformat() and parsed["total_days"] == sum(m["duration_days"] for m in ms) and all(m["planned_date"] == p["planned_date"].isoformat() for m, p in zip(ms, planned, strict=True))
            return consistent and parsed == effect and effect["organization_id"] == org and effect["order_id"] == order_id and effect["principal"] == actor and effect["target_arrival_date"] == payload["target_arrival_date"] and effect["transport_method_code"] == payload["transport_method_code"]
        except ValueError:
            return False
    return False


async def validate_receipt(session, receipt):
    def invalid():
        raise HTTPException(409, "Stored edit receipt is inconsistent")
    try:
        cmd = EditCommand.model_validate(receipt.command)
    except ValueError:
        invalid()
    c = common(receipt.organization_id, receipt.actor, cmd)
    if (cmd.model_dump(mode="json") != receipt.command or c["command_hash"] != receipt.command_hash
            or cmd.request_key != receipt.request_key or cmd.order_id != receipt.target_order_id or cmd.action != receipt.action):
        invalid()
    result = receipt.result
    if receipt.outcome == "rejected":
        if (not isinstance(result, dict) or result.get("code") not in CODES or receipt.ownership_id is not None
                or result != {**c, "outcome": "rejected", "code": result.get("code"), "no_business_write": True}):
            invalid()
        return
    owner = await session.get(PurchaseOwnership, receipt.ownership_id) if receipt.ownership_id else None
    if (receipt.outcome != "applied" or owner is None or owner.organization_id != receipt.organization_id
            or owner.kind != "order" or owner.source_id != cmd.order_id
            or not exact(result, [*c, "outcome", "ownership_id", "effect"])
            or result != {**c, "outcome": "applied", "ownership_id": owner.id, "effect": result.get("effect")}
            or not valid_effect(cmd.action, cmd.payload, result.get("effect"), receipt.organization_id, cmd.order_id, receipt.actor)):
        invalid()


async def saved(session, org, actor, command):
    row = await session.scalar(select(PurchaseOrderEditCommand).where(
        PurchaseOrderEditCommand.organization_id == org, PurchaseOrderEditCommand.request_key == command.request_key))
    if row is None:
        return None
    if row.actor != actor:
        raise HTTPException(403, "Command belongs to another principal")
    if row.command != command.model_dump(mode="json"):
        raise HTTPException(409, "Command key belongs to another payload")
    await validate_receipt(session, row)
    return row.result


async def persist(session, org, actor, cmd, *, code=None, owner=None, effect=None):
    # SQL effect evidence must exist before receipt insertion. ORM flush orders
    # inserts before deletes unless the business mutations are flushed first.
    await session.flush()
    c = common(org, actor, cmd)
    result = ({**c, "outcome": "rejected", "code": code, "no_business_write": True} if code else
              {**c, "outcome": "applied", "ownership_id": owner.id, "effect": effect})
    row = PurchaseOrderEditCommand(organization_id=org, request_key=cmd.request_key, actor=actor,
        target_order_id=cmd.order_id, action=cmd.action, outcome=result["outcome"], ownership_id=owner.id if owner else None,
        command=cmd.model_dump(mode="json"), command_hash=c["command_hash"], result=result)
    session.add(row)
    await session.flush()
    await validate_receipt(session, row)
    return result


def response(result):
    return JSONResponse(status_code=200 if result["outcome"] == "applied" else 409, content=result)


def check_target(command, order_id):
    if command.order_id != order_id:
        raise HTTPException(409, "Command target differs from path")


@router.post("/organizations/{org_id}/orders/{order_id}/edit-commands/reconcile")
async def reconcile(org_id: int, order_id: int, command: EditCommand, ctx=Depends(writer)):
    session, actor = ctx
    check_target(command, order_id)
    result = await saved(session, org_id, actor, command)
    if result is None:
        result = await persist(session, org_id, actor, command, code="command_abandoned")
    return response(result)


class RecordingBus:
    def __init__(self, bus):
        self.bus, self.events = bus, []

    def emit(self, session, *args, **kwargs):
        before = set(session.new)
        self.bus.emit(session, *args, **kwargs)
        self.events.extend(x for x in session.new if x not in before and isinstance(x, OutboxEvent))


@router.post("/organizations/{org_id}/orders/{order_id}/edit-commands")
async def execute(org_id: int, order_id: int, command: EditCommand, ctx=Depends(writer), core=Depends(get_core)):
    session, actor = ctx
    check_target(command, order_id)
    existing = await saved(session, org_id, actor, command)
    if existing is not None:
        return response(existing)
    owner = await session.scalar(select(PurchaseOwnership).where(PurchaseOwnership.organization_id == org_id,
        PurchaseOwnership.kind == "order", PurchaseOwnership.source_id == order_id))
    row = await session.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order_id).with_for_update().execution_options(populate_existing=True)) if owner else None
    code = None
    if row is None:
        code = "source_unavailable"
    elif command.action in {"add_line", "delete_line", "header"} and row.status in {"received", "cancelled"}:
        code = "order_not_editable"
    line = None
    if not code and command.action == "delete_line":
        line = await session.get(PurchaseOrderLine, command.payload["line_id"])
        if line is None or line.order_id != order_id:
            code = "line_unavailable"
    if not code and command.action == "status":
        try:
            routes._validate_transition(row.status, command.payload["status"])
        except HTTPException:
            code = "transition_not_allowed"
    if not code and command.action == "plan":
        method = await session.scalar(select(TransportMethod).where(TransportMethod.code == command.payload["transport_method_code"]))
        if method is None and command.payload["transport_method_code"] not in DEFAULT_METHODS:
            code = "transport_method_unavailable"
    if code:
        return response(await persist(session, org_id, actor, command, code=code))
    payload = INPUTS[command.action].model_validate(command.payload)
    if command.action == "add_line":
        ack = await routes.apply_add_line(order_id, payload, session, org_id, actor)
        effect = {"line": line_result(await session.get(PurchaseOrderLine, ack["affected_line_id"]))}
    elif command.action == "delete_line":
        effect = {"line": line_result(line)}
        await routes.apply_delete_line(order_id, line.id, session, org_id, actor)
    elif command.action == "header":
        def scalar(value):
            return str(value) if isinstance(value, Decimal) else value.isoformat() if hasattr(value, "isoformat") else value
        before = {k: scalar(getattr(row, k)) for k in command.payload}
        await routes.apply_order_header(order_id, payload, session, org_id, actor)
        effect = {"before": before, "after": command.payload}
    elif command.action == "status":
        previous = row.status
        recorder = RecordingBus(core.event_bus)
        await routes.apply_order_status(order_id, payload, SimpleNamespace(event_bus=recorder), session, org_id, actor)
        await session.flush()
        effect = {"from": previous, "to": row.status, "received_at": row.received_at.isoformat() if row.received_at else None,
                  "event_ids": [x.id for x in recorder.events]}
    else:
        effect = (await routes.apply_order_plan(order_id, payload, session, org_id, actor)).model_dump(mode="json")
    return response(await persist(session, org_id, actor, command, owner=owner, effect=effect))
