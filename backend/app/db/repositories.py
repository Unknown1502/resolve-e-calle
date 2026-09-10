"""Data access, including the optimistic-concurrency state transition.

The single most important function here is
:func:`transition_exception`. Every state change in the system goes
through it, and it implements ``spec.md`` §6 literally:

    UPDATE exceptions SET state = :new, version = version + 1
    WHERE id = :id AND version = :expected

    If zero rows are updated, reload state and re-evaluate instead of
    forcing the transition.

Combined with the state machine's terminal-immutability rule, that is
what makes "a late call result cannot overwrite an operator's manual
resolution" true rather than merely intended.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.db.models import (
    AuditEvent,
    CallAttempt,
    CallBatch,
    ExceptionRecord,
    OutboxMessage,
    PolicyDecisionRecord,
    WebhookEvent,
    WorkflowRun,
)
from app.domain.enums import (
    TERMINAL_EXCEPTION_STATES,
    ActorType,
    CallAttemptStatus,
    ExceptionState,
)
from app.domain.errors import ConcurrencyConflict
from app.domain.models import ExceptionSnapshot, PolicyDecision
from app.domain.state_machine import assert_can_transition


def _now() -> datetime:
    return datetime.now(UTC)


# ── snapshots ────────────────────────────────────────────────────────


def to_snapshot(record: ExceptionRecord, session: Session) -> ExceptionSnapshot:
    """Build the immutable view the policy engine consumes."""
    attempt_count = (
        session.execute(
            select(func.count(CallAttempt.id))
            .join(WorkflowRun, CallAttempt.workflow_run_id == WorkflowRun.id)
            .where(WorkflowRun.exception_id == record.id)
        ).scalar()
        or 0
    )
    last = session.execute(
        select(CallAttempt)
        .join(WorkflowRun, CallAttempt.workflow_run_id == WorkflowRun.id)
        .where(WorkflowRun.exception_id == record.id)
        .order_by(CallAttempt.attempt_no.desc())
        .limit(1)
    ).scalar_one_or_none()

    return ExceptionSnapshot(
        exception_id=record.id,
        po_number=record.po_number,
        supplier_name=record.supplier_name,
        recipient_name=record.recipient_name,
        recipient_phone_e164=record.recipient_phone_e164,
        ack_due_at=_aware(record.ack_due_at),
        expected_ship_date=record.expected_ship_date,
        state=ExceptionState(record.state),
        version=record.version,
        attempt_count=attempt_count,
        last_attempt_at=_aware(last.dispatched_at) if last and last.dispatched_at else None,
        last_attempt_status=CallAttemptStatus(last.status) if last else None,
        recipient_blocklisted=record.recipient_blocklisted,
    )


def _aware(dt: datetime) -> datetime:
    """Postgres may hand back naive datetimes depending on the driver."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ── the transition ───────────────────────────────────────────────────


def transition_exception(
    session: Session,
    exception_id: str,
    *,
    expected_version: int,
    new_state: ExceptionState,
    actor: ActorType,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> ExceptionRecord:
    """Move an exception to ``new_state`` under an optimistic version check.

    Raises:
        TerminalStateProtected: the exception is already terminal.
        InvalidTransition: the move is not legal.
        ConcurrencyConflict: someone else changed the row first. The
            caller must reload and re-decide -- never force the write.
    """
    record = session.get(ExceptionRecord, exception_id)
    if record is None:
        raise ConcurrencyConflict(f"exception {exception_id} disappeared")

    current = ExceptionState(record.state)
    # Legality first: this raises TerminalStateProtected for a late
    # result, which callers treat as benign.
    assert_can_transition(current, new_state)

    # execute() is typed as Result, but an UPDATE always yields a
    # CursorResult -- and rowcount is the whole point of this call: it
    # is how the optimistic version check reports a lost race.
    result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
        update(ExceptionRecord)
        .where(
            ExceptionRecord.id == exception_id,
            ExceptionRecord.version == expected_version,
        )
        .values(state=new_state.value, version=ExceptionRecord.version + 1, updated_at=_now())
    )

    if result.rowcount == 0:
        raise ConcurrencyConflict(
            f"exception {exception_id} was modified concurrently "
            f"(expected version {expected_version})",
            context={"exception_id": exception_id, "expected_version": expected_version},
        )

    session.add(
        AuditEvent(
            entity_type="exception",
            entity_id=exception_id,
            event_type=event_type,
            actor_type=actor.value,
            previous_state=current.value,
            new_state=new_state.value,
            payload=payload or {},
        )
    )
    session.flush()
    session.refresh(record)
    return record


def record_audit(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor: ActorType,
    payload: dict[str, Any] | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor.value,
        previous_state=previous_state,
        new_state=new_state,
        payload=payload or {},
    )
    session.add(event)
    return event


def audit_event_exists(session: Session, entity_id: str, provider_event_id: str) -> bool:
    """True when a provider event has already been folded into the trail.

    Provider events are immutable, so their id is a stable dedupe key
    across repeated reconciliations of the same call.
    """
    return (
        session.execute(
            select(AuditEvent.id).where(
                AuditEvent.entity_id == entity_id,
                AuditEvent.payload["provider_event_id"].astext == provider_event_id,
            ).limit(1)
        ).scalar_one_or_none()
        is not None
    )


def record_policy_decision(
    session: Session,
    decision: PolicyDecision,
    *,
    exception_id: str | None = None,
    workflow_run_id: str | None = None,
    call_attempt_id: str | None = None,
) -> PolicyDecisionRecord:
    record = PolicyDecisionRecord(
        exception_id=exception_id,
        workflow_run_id=workflow_run_id,
        call_attempt_id=call_attempt_id,
        decision=decision.decision.value,
        reason_code=decision.reason_code.value,
        reason_text=decision.reason_text,
        input_snapshot=decision.input_snapshot,
    )
    session.add(record)
    return record


# ── queries ──────────────────────────────────────────────────────────


def list_exceptions(session: Session, *, states: list[str] | None = None) -> list[ExceptionRecord]:
    stmt = select(ExceptionRecord).order_by(ExceptionRecord.ack_due_at.asc())
    if states:
        stmt = stmt.where(ExceptionRecord.state.in_(states))
    return list(session.execute(stmt).scalars())


def find_callable_exceptions(
    session: Session, *, limit: int = 25, claim: bool = False
) -> list[ExceptionRecord]:
    """Exceptions the scanner may drive toward a call.

    Args:
        claim: take a row lock on each match, skipping rows another
            transaction already holds. Callers that are about to
            *dispatch* must pass ``True``.

    Why the lock matters: the API and the worker both look for callable
    exceptions. Without it they can select overlapping sets, and because
    two batches with different membership derive *different* idempotency
    keys, the provider has no way to recognise them as the same work.
    The result is two real phone calls to one supplier -- the exact
    failure the rest of this design exists to prevent.
    ``docs/architecture/low-level.md``: "database row locks for stateful
    operations".
    """
    stmt = (
        select(ExceptionRecord)
        .where(
            ExceptionRecord.state.in_(
                [ExceptionState.OPEN.value, ExceptionState.RETRY_PENDING.value]
            ),
            ExceptionRecord.ack_due_at <= _now(),
            ExceptionRecord.recipient_blocklisted.is_(False),
        )
        .order_by(ExceptionRecord.ack_due_at.asc())
        .limit(limit)
    )
    if claim:
        stmt = stmt.with_for_update(skip_locked=True)
    return list(session.execute(stmt).scalars())


def lock_exception(session: Session, exception_id: str) -> ExceptionRecord | None:
    """Load one exception under a row lock, or ``None`` if another
    transaction already holds it.

    ``skip_locked`` rather than blocking: if a second planner is already
    working on this exception, the right answer is to leave it alone,
    not to queue up behind it and then dispatch a duplicate.
    """
    return session.execute(
        select(ExceptionRecord)
        .where(ExceptionRecord.id == exception_id)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def get_or_create_run(session: Session, exception_id: str) -> WorkflowRun:
    """Return the live run for an exception, creating one if needed."""
    existing = session.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.exception_id == exception_id,
            WorkflowRun.state.notin_(["COMPLETED", "ABANDONED"]),
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run = WorkflowRun(exception_id=exception_id, started_at=_now())
    session.add(run)
    session.flush()
    return run


def next_attempt_no(session: Session, workflow_run_id: str) -> int:
    highest = (
        session.execute(
            select(func.max(CallAttempt.attempt_no)).where(
                CallAttempt.workflow_run_id == workflow_run_id
            )
        ).scalar()
        or 0
    )
    return highest + 1


def attempts_for_batch(session: Session, batch_id: str) -> list[CallAttempt]:
    return list(
        session.execute(
            select(CallAttempt).where(CallAttempt.call_batch_id == batch_id)
        ).scalars()
    )


def batch_by_provider_call_id(session: Session, provider_call_id: str) -> CallBatch | None:
    return session.execute(
        select(CallBatch).where(CallBatch.provider_call_id == provider_call_id)
    ).scalar_one_or_none()


def batch_by_idempotency_key(session: Session, key: str) -> CallBatch | None:
    return session.execute(
        select(CallBatch).where(CallBatch.idempotency_key == key)
    ).scalar_one_or_none()


def stale_calling_batches(session: Session, *, older_than: datetime) -> list[CallBatch]:
    """Dispatched batches with no terminal result yet -- reconcile these."""
    return list(
        session.execute(
            select(CallBatch).where(
                CallBatch.status == "DISPATCHED",
                CallBatch.dispatched_at.isnot(None),
                CallBatch.dispatched_at <= older_than,
            )
        ).scalars()
    )


def audit_trail(session: Session, exception_id: str) -> list[AuditEvent]:
    """Every audited event that bears on one exception, in time order.

    Deliberately spans three entity types. Transitions are recorded
    against the exception, dispatch against the workflow run, and
    CALL-E's own developer events against the *batch* -- which is a
    different row, because one batch covers several exceptions. Querying
    only the exception silently drops the provider's view of the call,
    which is the half an operator most wants when the agent's conclusion
    surprises them.
    """
    run_ids = [
        r
        for (r,) in session.execute(
            select(WorkflowRun.id).where(WorkflowRun.exception_id == exception_id)
        )
    ]
    batch_ids = [
        b
        for (b,) in session.execute(
            select(CallAttempt.call_batch_id)
            .join(WorkflowRun, CallAttempt.workflow_run_id == WorkflowRun.id)
            .where(
                WorkflowRun.exception_id == exception_id,
                CallAttempt.call_batch_id.isnot(None),
            )
            .distinct()
        )
    ]
    attempt_ids = [
        a
        for (a,) in session.execute(
            select(CallAttempt.id)
            .join(WorkflowRun, CallAttempt.workflow_run_id == WorkflowRun.id)
            .where(WorkflowRun.exception_id == exception_id)
        )
    ]
    return list(
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.entity_id.in_(
                    [exception_id, *run_ids, *batch_ids, *attempt_ids]
                )
            )
            .order_by(AuditEvent.created_at.asc())
        ).scalars()
    )


def decisions_for(session: Session, exception_id: str) -> list[PolicyDecisionRecord]:
    return list(
        session.execute(
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.exception_id == exception_id)
            .order_by(PolicyDecisionRecord.created_at.asc())
        ).scalars()
    )


# ── webhook dedupe ───────────────────────────────────────────────────


def payload_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def register_webhook_event(
    session: Session,
    *,
    provider_event_id: str,
    provider_call_id: str | None,
    event_type: str | None,
    body: dict[str, Any],
) -> tuple[WebhookEvent, bool]:
    """Persist a webhook event. Returns ``(event, is_new)``.

    Duplicate deliveries are expected -- CALL-E delivery is
    at-least-once. The UNIQUE constraint on ``provider_event_id`` is the
    dedupe mechanism; this function reads it back rather than racing on
    a prior SELECT.
    """
    existing = session.execute(
        select(WebhookEvent).where(WebhookEvent.provider_event_id == provider_event_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    event = WebhookEvent(
        provider_event_id=provider_event_id,
        provider_call_id=provider_call_id,
        event_type=event_type,
        payload_hash=payload_hash(body),
    )
    session.add(event)
    session.flush()
    return event, True


# ── outbox ───────────────────────────────────────────────────────────


def enqueue(
    session: Session,
    *,
    dedupe_key: str,
    event_type: str,
    payload: dict[str, Any],
    available_at: datetime | None = None,
) -> OutboxMessage | None:
    """Add a side effect to the outbox, ignoring exact duplicates.

    Returns ``None`` when the key already exists -- that is the
    idempotency guarantee working, not an error.
    """
    existing = session.execute(
        select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return None

    message = OutboxMessage(
        dedupe_key=dedupe_key,
        event_type=event_type,
        payload=payload,
        available_at=available_at or _now(),
    )
    session.add(message)
    session.flush()
    return message


def claim_outbox(session: Session, *, limit: int = 10) -> list[OutboxMessage]:
    """Claim pending outbox rows for this worker.

    ``FOR UPDATE SKIP LOCKED`` so several workers can drain the outbox
    concurrently without handing the same side effect to two of them.
    """
    rows = list(
        session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.status == "PENDING",
                OutboxMessage.available_at <= _now(),
            )
            .order_by(OutboxMessage.available_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    for row in rows:
        row.status = "IN_FLIGHT"
        row.attempts += 1
    session.flush()
    return rows


def mark_delivered(session: Session, message: OutboxMessage) -> None:
    message.status = "DELIVERED"
    message.delivered_at = _now()


def mark_failed(
    session: Session, message: OutboxMessage, error: str, *, retry_in_s: int = 30
) -> None:
    from datetime import timedelta

    message.status = "PENDING"
    message.last_error = error[:2000]
    message.available_at = _now() + timedelta(seconds=retry_in_s)


def is_terminal_state(state: str) -> bool:
    return state in {s.value for s in TERMINAL_EXCEPTION_STATES}
