"""HTTP endpoints.

Implements ``docs/api-contract.md``. The webhook receiver deserves the
most attention: it is deliberately the dumbest endpoint in the system.

    validate shape -> dedupe by event id -> store metadata
    -> enqueue reconciliation -> 2xx

It runs no business logic, because CALL-E retries non-2xx deliveries and
a slow or throwing receiver turns one late call into a redelivery storm.
The decision happens in the reconciliation worker, against authoritative
provider state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.calle.mapper import extract_webhook_call
from app.api.schemas import (
    AttemptView,
    AuditView,
    BatchView,
    CreateExceptionRequest,
    CreateExceptionResponse,
    DecisionView,
    ExceptionDetail,
    ExceptionSummary,
    HealthResponse,
    ResolveRequest,
    ResolveResponse,
    mask_phone,
)
from app.config import get_settings
from app.db import repositories as repo
from app.db.models import CallAttempt, CallBatch, ExceptionRecord, WorkflowRun
from app.db.session import get_db
from app.domain.enums import ActorType, ExceptionState, ReasonCode
from app.domain.errors import ResolveEError
from app.services.call_orchestrator import resolve_exceptions

log = logging.getLogger(__name__)

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ── health ───────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        log.error("health: database unreachable", extra={"error": str(exc)})
        database = "unavailable"

    # safe_dump() never includes the API key itself.
    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        database=database,
        config=get_settings().safe_dump(),
    )


# ── exceptions ───────────────────────────────────────────────────────


@router.post(
    "/api/exceptions",
    response_model=CreateExceptionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["exceptions"],
)
def create_exception(
    payload: CreateExceptionRequest, db: Session = Depends(get_db)
) -> CreateExceptionResponse:
    existing = (
        db.query(ExceptionRecord).filter(ExceptionRecord.po_number == payload.po_number).first()
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{payload.po_number} already exists")

    record = ExceptionRecord(
        po_number=payload.po_number,
        supplier_name=payload.supplier_name,
        recipient_name=payload.recipient_name,
        recipient_phone_e164=payload.recipient_phone_e164,
        ack_due_at=payload.ack_due_at,
        expected_ship_date=payload.expected_ship_date,
        item_summary=payload.item_summary,
        state=ExceptionState.OPEN,
    )
    db.add(record)
    db.flush()

    repo.record_audit(
        db,
        entity_type="exception",
        entity_id=record.id,
        event_type="exception.created",
        actor=ActorType.OPERATOR,
        new_state=ExceptionState.OPEN.value,
        payload={"po_number": record.po_number, "supplier_name": record.supplier_name},
    )
    return CreateExceptionResponse(exception_id=record.id, state=record.state)


@router.get("/api/exceptions", response_model=list[ExceptionSummary], tags=["exceptions"])
def list_exceptions(
    state: list[str] | None = Query(default=None), db: Session = Depends(get_db)
) -> list[ExceptionSummary]:
    records = repo.list_exceptions(db, states=state)
    return [_summary(db, r) for r in records]


@router.get("/api/exceptions/{exception_id}", response_model=ExceptionDetail, tags=["exceptions"])
def get_exception(exception_id: str, db: Session = Depends(get_db)) -> ExceptionDetail:
    record = db.get(ExceptionRecord, exception_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "exception not found")

    runs = db.query(WorkflowRun).filter(WorkflowRun.exception_id == exception_id).all()
    run_ids = [r.id for r in runs]
    attempts = (
        db.query(CallAttempt)
        .filter(CallAttempt.workflow_run_id.in_(run_ids or [""]))
        .order_by(CallAttempt.attempt_no.asc())
        .all()
    )
    batch_ids = {a.call_batch_id for a in attempts if a.call_batch_id}
    batches = (
        db.query(CallBatch).filter(CallBatch.id.in_(batch_ids)).all() if batch_ids else []
    )

    base = _summary(db, record)
    return ExceptionDetail(
        **base.model_dump(),
        recipient_phone_masked=mask_phone(record.recipient_phone_e164),
        item_summary=record.item_summary,
        attempts=[AttemptView.model_validate(a) for a in attempts],
        batches=[BatchView.model_validate(b) for b in batches],
        decisions=[DecisionView.model_validate(d) for d in repo.decisions_for(db, exception_id)],
        audit=[AuditView.model_validate(a) for a in repo.audit_trail(db, exception_id)],
    )


@router.post(
    "/api/exceptions/{exception_id}/resolve",
    response_model=ResolveResponse,
    tags=["exceptions"],
)
def resolve_one(exception_id: str, db: Session = Depends(get_db)) -> ResolveResponse:
    if db.get(ExceptionRecord, exception_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "exception not found")
    return _resolve(db, [exception_id])


@router.post("/api/exceptions/resolve", response_model=ResolveResponse, tags=["exceptions"])
def resolve_many(payload: ResolveRequest, db: Session = Depends(get_db)) -> ResolveResponse:
    """Resolve a set of exceptions in ONE CALL-E call task.

    This is the batch path: `recipients[]` plus
    `recipient_result_schema` gives each supplier an independent outcome
    from a single provider call.
    """
    ids = payload.exception_ids
    if not ids:
        ids = [r.id for r in repo.find_callable_exceptions(db, claim=True)]
    if not ids:
        return ResolveResponse(message="No exceptions are currently eligible for a call.")
    return _resolve(db, ids)


def _resolve(db: Session, exception_ids: list[str]) -> ResolveResponse:
    try:
        batch = resolve_exceptions(db, exception_ids)
    except ResolveEError as exc:
        # Classified failure. Surface the class, never the stack.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {"error_class": str(exc.error_class), "message": exc.message},
        ) from exc

    if batch is None:
        return ResolveResponse(
            message="No call was placed. Policy refused every requested exception."
        )

    attempts = repo.attempts_for_batch(db, batch.id)
    return ResolveResponse(
        batch_id=batch.id,
        provider_call_id=batch.provider_call_id,
        is_live=batch.is_live,
        dispatched=len(attempts),
        message=(
            f"Created 1 CALL-E call task covering {len(attempts)} supplier(s)."
            if batch.is_live
            else f"Created 1 simulated call task covering {len(attempts)} supplier(s). "
            "Set CALLE_LIVE_CALLS=true for real calls."
        ),
    )


#: Gate checks that every call passes on its way out. They are recorded
#: for the audit trail, but showing one in the queue tells an operator
#: nothing about what happened -- the interesting decision is the one
#: that actually moved the workflow.
_ROUTINE_PASSES = frozenset(
    {
        ReasonCode.ELIGIBLE_FOR_CALL.value,
        ReasonCode.ATTEMPT_BUDGET_AVAILABLE.value,
        ReasonCode.TASK_WITHIN_DISCLOSURE_BUDGET.value,
    }
)


def _headline_reason(decisions: list) -> str | None:
    """The most recent decision worth showing a human."""
    for d in reversed(decisions):
        if d.reason_code not in _ROUTINE_PASSES:
            return d.reason_text
    return decisions[-1].reason_text if decisions else None


def _summary(db: Session, record: ExceptionRecord) -> ExceptionSummary:
    snapshot = repo.to_snapshot(record, db)
    overdue = (_now() - _aware(record.ack_due_at)).total_seconds() / 3600
    decisions = repo.decisions_for(db, record.id)
    headline = _headline_reason(decisions)
    last_batch = (
        db.query(CallBatch)
        .join(CallAttempt, CallAttempt.call_batch_id == CallBatch.id)
        .join(WorkflowRun, CallAttempt.workflow_run_id == WorkflowRun.id)
        .filter(WorkflowRun.exception_id == record.id)
        .order_by(CallBatch.created_at.desc())
        .first()
    )
    return ExceptionSummary(
        id=record.id,
        po_number=record.po_number,
        supplier_name=record.supplier_name,
        recipient_name=record.recipient_name,
        state=record.state,
        version=record.version,
        ack_due_at=_aware(record.ack_due_at),
        expected_ship_date=record.expected_ship_date,
        attempt_count=snapshot.attempt_count,
        hours_overdue=round(max(overdue, 0.0), 1),
        last_reason_text=headline,
        is_live=bool(last_batch.is_live) if last_batch else False,
    )


# ── webhook ──────────────────────────────────────────────────────────


@router.post("/webhooks/calle", tags=["webhooks"], status_code=status.HTTP_200_OK)
def calle_webhook(
    request: Request,
    body: dict[str, Any] = Body(...),
    calle_event_id: str | None = Header(default=None, alias="CALL-E-Event-Id"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Receive a terminal CALL-E event.

    Stays lightweight on purpose (``docs/architecture/low-level.md``).
    It does not decide anything -- it records the event and enqueues
    reconciliation, then returns 2xx quickly so CALL-E stops retrying.
    """
    try:
        event_id, event_type, call = extract_webhook_call(body)
    except ValueError as exc:
        # Malformed: reject without touching state. A 400 here is
        # correct -- redelivering the same broken body will not help.
        log.warning("webhook rejected", extra={"reason": str(exc)})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed webhook: {exc}") from exc

    # The header is required by the spec; when both are present they
    # must agree, or we are being replayed something stitched together.
    if calle_event_id and calle_event_id != event_id:
        log.warning(
            "webhook event id mismatch",
            extra={"header": calle_event_id, "body": event_id},
        )

    event, is_new = repo.register_webhook_event(
        db,
        provider_event_id=event_id,
        provider_call_id=call.provider_call_id,
        event_type=event_type,
        body=body,
    )

    if not is_new:
        # At-least-once delivery. Duplicates are expected and harmless.
        log.info("duplicate webhook ignored", extra={"event_id": event_id})
        return {"ok": True, "duplicate": True, "event_id": event_id}

    repo.enqueue(
        db,
        dedupe_key=f"reconcile:{call.provider_call_id}:{event_id}",
        event_type="reconcile_call",
        payload={
            "provider_call_id": call.provider_call_id,
            "event_type": event_type,
            "event_id": event_id,
        },
    )
    event.processed_at = _now()

    repo.record_audit(
        db,
        entity_type="call_batch",
        entity_id=call.provider_call_id,
        event_type=f"webhook.{event_type}",
        actor=ActorType.PROVIDER,
        payload={"event_id": event_id, "provider_status": call.status},
    )

    return {"ok": True, "duplicate": False, "event_id": event_id}
