"""Turn one terminal call task into N independent workflow decisions.

This is where batch calling pays off and where it could most easily go
wrong. One CALL-E call task carries several suppliers; each maps back to
its own exception, gets its own policy evaluation, and lands in its own
state. A confident answer from supplier A must never influence the
outcome for supplier B.

Correlation is by ``provider_recipient_id``, recorded at dispatch. If a
recipient cannot be correlated the result is recorded as unattributed
evidence and nothing is transitioned -- guessing which exception a
result belongs to would be worse than leaving it for a human.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import repositories as repo
from app.db.models import CallAttempt, CallBatch, ExceptionRecord
from app.domain.enums import (
    ActorType,
    BatchStatus,
    CallAttemptStatus,
    Decision,
    ExceptionState,
    ReasonCode,
)
from app.domain.errors import ConcurrencyConflict, TerminalStateProtected
from app.domain.models import NormalizedCall, PolicyDecision
from app.policies.transition_policy import DECISION_TO_STATE, decide

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def persist_call_snapshot(session: Session, batch: CallBatch, call: NormalizedCall) -> None:
    """Record the terminal provider snapshot on the batch."""
    batch.provider_call_id = call.provider_call_id
    batch.task_structured_result = call.raw_structured_result
    batch.summary = call.summary
    batch.task_completed = call.task_completed
    if call.completion_confidence is not None:
        batch.completion_confidence_score = call.completion_confidence.score
        batch.completion_confidence_label = call.completion_confidence.label
    batch.evidence = list(call.evidence)
    # Verbatim, for support. Never branched on.
    batch.failure_code = call.failure_code
    batch.failure_message = call.failure_message
    if call.is_terminal:
        batch.status = BatchStatus.TERMINAL if call.status != "failed" else BatchStatus.FAILED
        batch.completed_at = call.completed_at or _now()


def process_terminal_call(
    session: Session,
    call: NormalizedCall,
    *,
    settings: Settings | None = None,
    reconciliation_available: bool = True,
    now: datetime | None = None,
) -> list[tuple[str, Decision]]:
    """Apply a terminal call task to every exception it covers.

    Returns:
        ``(exception_id, decision)`` for each attempt processed.
    """
    settings = settings or get_settings()
    policy = settings.policy()
    now = now or _now()

    batch = repo.batch_by_provider_call_id(session, call.provider_call_id)
    if batch is None:
        log.warning("no batch for provider call", extra={"provider_call_id": call.provider_call_id})
        return []

    persist_call_snapshot(session, batch, call)

    if not call.is_terminal:
        # Not terminal: record the snapshot and wait. Never decide.
        log.info(
            "call not terminal; deferring",
            extra={"provider_call_id": call.provider_call_id, "status": call.status},
        )
        session.flush()
        return []

    outcomes: list[tuple[str, Decision]] = []

    for attempt in repo.attempts_for_batch(session, batch.id):
        result = _process_attempt(
            session,
            attempt,
            call,
            settings=settings,
            policy=policy,
            reconciliation_available=reconciliation_available,
            now=now,
        )
        if result is not None:
            outcomes.append(result)

    session.flush()
    return outcomes


def _process_attempt(
    session: Session,
    attempt: CallAttempt,
    call: NormalizedCall,
    *,
    settings: Settings,
    policy,
    reconciliation_available: bool,
    now: datetime,
) -> tuple[str, Decision] | None:
    run = attempt.run
    record = session.get(ExceptionRecord, run.exception_id)
    if record is None:
        return None

    recipient = (
        call.recipient_by_id(attempt.provider_recipient_id)
        if attempt.provider_recipient_id
        else None
    )

    if recipient is None:
        # The terminal call carries no slice for this attempt. We must
        # not invent an outcome -- but leaving the attempt DISPATCHED
        # would strand the exception in CALLING forever, because the
        # batch is now terminal and the reconciliation sweep only looks
        # at batches still marked DISPATCHED.
        #
        # So: fail the attempt and release the exception back to
        # RETRY_PENDING, where the scanner can pick it up within the
        # attempt budget. No evidence is fabricated; the workflow simply
        # stays alive.
        attempt.status = CallAttemptStatus.FAILED
        attempt.failure_message = (
            "No recipient slice for this attempt in the terminal call task."
        )
        attempt.completed_at = now
        repo.record_audit(
            session,
            entity_type="call_attempt",
            entity_id=attempt.id,
            event_type="attempt.unattributed_result",
            actor=ActorType.PROVIDER,
            payload={
                "provider_call_id": call.provider_call_id,
                "expected_recipient_id": attempt.provider_recipient_id,
                "recipients_in_call": [r.provider_recipient_id for r in call.recipients],
            },
        )
        log.warning(
            "could not correlate recipient; releasing exception for retry",
            extra={
                "attempt_id": attempt.id,
                "provider_call_id": call.provider_call_id,
                "exception_id": record.id,
            },
        )

        decision = PolicyDecision(
            decision=Decision.RETRY,
            reason_code=ReasonCode.RECIPIENT_NOT_REACHED,
            reason_text=(
                "The terminal CALL-E call task contained no result for this supplier, "
                "so no conversation can be attributed to them. Releasing the exception "
                "to be retried rather than recording an outcome."
            ),
            input_snapshot={
                "exception_id": record.id,
                "provider_call_id": call.provider_call_id,
                "expected_recipient_id": attempt.provider_recipient_id,
            },
        )
        repo.record_policy_decision(
            session,
            decision,
            exception_id=record.id,
            workflow_run_id=run.id,
            call_attempt_id=attempt.id,
        )
        _apply(session, record, run, decision, call, repo.to_snapshot(record, session))
        return (record.id, Decision.RETRY)

    # Persist this supplier's slice before deciding, so the evidence is
    # durable even if the decision path raises.
    attempt.structured_result = recipient.raw_structured_result
    attempt.summary = recipient.summary
    attempt.recipient_status = recipient.status
    attempt.transcript = [
        {"speaker": t.speaker, "text": t.text, "offset_seconds": t.offset_seconds}
        for t in recipient.transcript
    ] or None
    # Provider diagnostics from the dial itself, for display only.
    if recipient.attempts:
        last = recipient.attempts[-1]
        attempt.failure_code = last.failure_code
        attempt.failure_message = last.failure_message
    attempt.completed_at = call.completed_at or now
    attempt.status = (
        CallAttemptStatus.COMPLETED if recipient.reached else CallAttemptStatus.FAILED
    )
    session.flush()

    snapshot = repo.to_snapshot(record, session)
    decision = decide(
        snapshot,
        call,
        recipient,
        policy,
        now=now,
        reconciliation_available=reconciliation_available,
    )

    repo.record_policy_decision(
        session,
        decision,
        exception_id=record.id,
        workflow_run_id=run.id,
        call_attempt_id=attempt.id,
    )

    if decision.suppress_future_calls and not record.recipient_blocklisted:
        # A request not to be called again outlives this workflow. The
        # blocklist is checked by call eligibility, so no later retry on
        # THIS exception can dial them back.
        #
        # Scope, stated precisely: `recipient_blocklisted` lives on
        # ExceptionRecord -- one row per purchase order -- not on the
        # phone number. A different PO to the same contact is NOT
        # protected by this. See docs/provider-truth.md §11 "Suppression
        # scope" and the regression test
        # test_reliability.py::test_suppression_is_scoped_to_the_exception_not_the_phone_number.
        # Extending this to a phone-number-level "do not call" registry
        # is a real, reasonable next step, not implemented here.
        record.recipient_blocklisted = True
        repo.record_audit(
            session,
            entity_type="exception",
            entity_id=record.id,
            event_type="recipient.suppressed",
            actor=ActorType.POLICY,
            payload={
                "reason_code": decision.reason_code.value,
                "provider_call_id": call.provider_call_id,
            },
        )
        log.info("recipient suppressed at their request", extra={"exception_id": record.id})

    _apply(session, record, run, decision, call, snapshot)
    return (record.id, decision.decision)


def _apply(session, record, run, decision, call, snapshot) -> None:
    """Move the exception according to a decision, safely."""
    target = DECISION_TO_STATE[decision.decision]
    if target is None:
        # NOOP: a late result on a terminal exception. Already audited
        # as a policy decision; leave the state alone.
        return

    payload = {
        "decision": decision.decision.value,
        "reason_code": decision.reason_code.value,
        "reason_text": decision.reason_text,
        "provider_call_id": call.provider_call_id,
    }

    try:
        # RESULT_RECEIVED -> EVIDENCE_VALIDATED -> terminal is the
        # documented path; walk it so the audit trail shows the whole
        # chain rather than a jump to the answer.
        current = ExceptionState(record.state)
        if current in (ExceptionState.CALLING, ExceptionState.RECONCILING):
            record = repo.transition_exception(
                session,
                record.id,
                expected_version=record.version,
                new_state=ExceptionState.RESULT_RECEIVED,
                actor=ActorType.PROVIDER,
                event_type="exception.result_received",
                payload={"provider_call_id": call.provider_call_id},
            )

        if target in (
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
            ExceptionState.RETRY_PENDING,
        ) and ExceptionState(record.state) == ExceptionState.RESULT_RECEIVED:
            record = repo.transition_exception(
                session,
                record.id,
                expected_version=record.version,
                new_state=ExceptionState.EVIDENCE_VALIDATED,
                actor=ActorType.POLICY,
                event_type="exception.evidence_validated",
                payload={"reason_code": decision.reason_code.value},
            )

        repo.transition_exception(
            session,
            record.id,
            expected_version=record.version,
            new_state=target,
            actor=ActorType.POLICY,
            event_type=f"exception.{target.value.lower()}",
            payload=payload,
        )

        if target in (
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        ):
            run.state = "COMPLETED"
            run.completed_at = _now()

    except TerminalStateProtected:
        # Reliability rule 8. The operator got there first; the call
        # result stays as historical evidence.
        repo.record_audit(
            session,
            entity_type="exception",
            entity_id=record.id,
            event_type="exception.late_result_ignored",
            actor=ActorType.SYSTEM,
            payload={**payload, "current_state": record.state},
        )
        log.info("late result ignored", extra={"exception_id": record.id, "state": record.state})

    except ConcurrencyConflict:
        # Someone changed the row between snapshot and write. Do not
        # force it -- record and let the next pass re-decide.
        repo.record_audit(
            session,
            entity_type="exception",
            entity_id=record.id,
            event_type="exception.concurrency_conflict",
            actor=ActorType.SYSTEM,
            payload={**payload, "expected_version": snapshot.version},
        )
        log.warning("concurrency conflict", extra={"exception_id": record.id})
