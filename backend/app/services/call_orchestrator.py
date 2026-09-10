"""Plan and dispatch call batches.

The dispatch path is the one place where a bug costs a real phone call
to a real person, so the ordering below is deliberate:

    eligibility -> attempt budget -> build task -> disclosure scan
    -> persist attempts + batch (committed) -> dispatch -> record ids

The batch row and its attempts are committed *before* the provider is
called. If the process dies mid-dispatch, the batch is still in the
database with its idempotency key, so the retry reuses that key and
CALL-E returns the original call instead of dialling anyone twice
(``reliability-and-failure.md`` §4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.calle.schemas import (
    RECIPIENT_RESULT_SCHEMA,
    SCHEMA_VERSION,
    TASK_RESULT_SCHEMA,
    operator_supplied_text,
    render_task,
)
from app.config import Settings, get_settings
from app.db import repositories as repo
from app.db.models import CallAttempt, CallBatch, ExceptionRecord
from app.domain.commands import (
    CallRecipientCommand,
    CreateCallCommand,
    compute_batch_idempotency_key,
)
from app.domain.enums import (
    ActorType,
    BatchStatus,
    CallAttemptStatus,
    Decision,
    ExceptionState,
)
from app.domain.errors import PolicyViolation, ProviderError, ResolveEError
from app.policies.attempt_policy import check_attempt_budget
from app.policies.call_eligibility import check_call_eligibility, refusal_needs_a_human
from app.policies.disclosure import check_disclosure_budget
from app.services.provider import select_provider

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class BatchPlan:
    """A validated set of exceptions ready to become one call task."""

    def __init__(self) -> None:
        self.recipients: list[CallRecipientCommand] = []
        self.attempts: list[CallAttempt] = []
        self.contexts: list[dict[str, str]] = []
        self.refused: list[tuple[str, str]] = []  # (exception_id, reason)

    def __bool__(self) -> bool:
        return bool(self.recipients)


def plan_batch(
    session: Session,
    exception_ids: list[str],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> BatchPlan:
    """Validate exceptions and stage attempt rows for a single call task.

    Exceptions that fail policy are refused individually and recorded;
    they never block the rest of the batch.
    """
    settings = settings or get_settings()
    policy = settings.policy()
    now = now or _now()
    plan = BatchPlan()

    for exception_id in exception_ids:
        # Claim the row for the whole of planning, so the eligibility
        # check and the CALL_PLANNED transition are atomic with respect
        # to any other planner. A row another transaction already holds
        # is skipped, not waited on.
        record = repo.lock_exception(session, exception_id)
        if record is None:
            plan.refused.append(
                (exception_id, "exception not found, or already claimed by another dispatcher")
            )
            continue

        snapshot = repo.to_snapshot(record, session)

        eligibility = check_call_eligibility(snapshot, policy, now=now)
        repo.record_policy_decision(session, eligibility, exception_id=exception_id)
        if not eligibility.allows_call:
            plan.refused.append((exception_id, eligibility.reason_text))
            # Temporal refusals -- not due yet, quiet hours -- leave the
            # exception exactly where it is for a later pass.
            if refusal_needs_a_human(eligibility.reason_code):
                _refuse(session, record, snapshot, eligibility.reason_text)
            continue

        budget = check_attempt_budget(snapshot, policy, now=now)
        repo.record_policy_decision(session, budget, exception_id=exception_id)
        if budget.decision != Decision.PROCEED_WITH_CALL:
            plan.refused.append((exception_id, budget.reason_text))
            if budget.decision == Decision.HUMAN_REVIEW:
                _refuse(session, record, snapshot, budget.reason_text)
            continue

        run = repo.get_or_create_run(session, exception_id)
        attempt_no = repo.next_attempt_no(session, run.id)

        attempt = CallAttempt(
            workflow_run_id=run.id,
            attempt_no=attempt_no,
            status=CallAttemptStatus.PENDING,
        )
        session.add(attempt)
        run.current_attempt_no = attempt_no

        plan.attempts.append(attempt)
        plan.recipients.append(
            CallRecipientCommand(
                workflow_run_id=run.id,
                exception_id=exception_id,
                attempt_no=attempt_no,
                po_number=record.po_number,
                supplier_name=record.supplier_name,
                recipient_name=record.recipient_name,
                phone_e164=record.recipient_phone_e164,
            )
        )
        plan.contexts.append(
            {
                "supplier_name": record.supplier_name,
                "recipient_name": record.recipient_name or "",
                "po_number": record.po_number,
                "phone": record.recipient_phone_e164,
            }
        )

        # OPEN/RETRY_PENDING -> ELIGIBLE -> CALL_PLANNED
        if snapshot.state in (ExceptionState.OPEN, ExceptionState.RETRY_PENDING):
            if snapshot.state == ExceptionState.OPEN:
                record = repo.transition_exception(
                    session,
                    exception_id,
                    expected_version=record.version,
                    new_state=ExceptionState.ELIGIBLE,
                    actor=ActorType.POLICY,
                    event_type="exception.eligible",
                    payload={"reason_code": eligibility.reason_code.value},
                )
            repo.transition_exception(
                session,
                exception_id,
                expected_version=record.version,
                new_state=ExceptionState.CALL_PLANNED,
                actor=ActorType.SYSTEM,
                event_type="exception.call_planned",
                payload={"attempt_no": attempt_no},
            )

    session.flush()
    return plan


def _refuse(session: Session, record: ExceptionRecord, snapshot, reason: str) -> None:
    """Send an exception a policy refused to human review."""
    from app.domain.errors import InvalidTransition

    try:
        repo.transition_exception(
            session,
            record.id,
            expected_version=record.version,
            new_state=ExceptionState.HUMAN_REVIEW,
            actor=ActorType.POLICY,
            event_type="exception.policy_refused",
            payload={"reason": reason},
        )
    except InvalidTransition as exc:
        # Already terminal, or not in a state that can escalate. Benign.
        log.info("refusal did not transition", extra={"exception_id": record.id, "why": str(exc)})


def dispatch_batch(
    session: Session,
    plan: BatchPlan,
    *,
    settings: Settings | None = None,
) -> CallBatch | None:
    """Turn a plan into one CALL-E call task.

    Returns the persisted :class:`CallBatch`, or ``None`` when the plan
    is empty or the disclosure gate refused the rendered task.
    """
    if not plan:
        return None

    settings = settings or get_settings()
    recipients = tuple(plan.recipients)

    if settings.live_calls_enabled and not settings.disclosure_ready:
        # Fail closed. An agent that cannot truthfully name the company
        # it represents must not place a real call, and a placeholder
        # would be spoken aloud to a person.
        log.error(
            "refusing live dispatch: BUYER_COMPANY is not configured",
            extra={"recipients": len(recipients)},
        )
        raise PolicyViolation(
            "buyer_company is not configured; the agent cannot truthfully say "
            "who it is calling for"
        )

    task_text = render_task(
        plan.contexts,
        buyer_company=settings.buyer_company or "an authorized buyer (simulated)",
    )

    # Scan the operator-supplied values that were interpolated into the
    # task -- not the whole rendered task. The template's own safety
    # rules name "password", "credential", "negotiate" and "price", so
    # scanning the rendered text makes every clean task block itself.
    disclosure = check_disclosure_budget(operator_supplied_text(plan.contexts))
    for rc in recipients:
        repo.record_policy_decision(session, disclosure, exception_id=rc.exception_id)
    if not disclosure.allows_call:
        log.warning(
            "dispatch blocked by disclosure budget",
            extra={"reason": disclosure.reason_text},
        )
        for rc in recipients:
            record = session.get(ExceptionRecord, rc.exception_id)
            if record is not None:
                _refuse(session, record, None, disclosure.reason_text)
        return None

    idempotency_key = compute_batch_idempotency_key(recipients)

    # A batch with this key may already exist if a previous dispatch
    # crashed after committing. Reuse it -- same key, same call.
    batch = repo.batch_by_idempotency_key(session, idempotency_key)
    if batch is None:
        batch = CallBatch(
            idempotency_key=idempotency_key,
            status=BatchStatus.PENDING,
            task_text=task_text,
        )
        session.add(batch)
        session.flush()

    for attempt in plan.attempts:
        attempt.call_batch_id = batch.id

    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch.id,
        "recipients": [
            {
                "exception_id": r.exception_id,
                "workflow_run_id": r.workflow_run_id,
                "attempt_no": r.attempt_no,
                "po_number": r.po_number,
            }
            for r in recipients
        ],
    }

    command = CreateCallCommand(
        recipients=recipients,
        task=task_text,
        result_schema=TASK_RESULT_SCHEMA,
        recipient_result_schema=RECIPIENT_RESULT_SCHEMA,
        idempotency_key=idempotency_key,
        webhook_url=settings.calle_webhook_url or None,
        metadata=metadata,
    )

    provider, is_live = select_provider(command, settings)
    batch.is_live = is_live

    # Everything above is committed before the provider is touched, so a
    # crash during dispatch is recoverable via the idempotency key.
    session.commit()

    try:
        handle = provider.create_call(command)
    except ResolveEError as exc:
        batch.status = BatchStatus.FAILED
        batch.failure_message = str(exc)[:2000]
        if isinstance(exc, ProviderError):
            batch.failure_code = exc.provider_code
        for attempt in plan.attempts:
            attempt.status = CallAttemptStatus.FAILED
            attempt.failure_message = str(exc)[:2000]
        repo.record_audit(
            session,
            entity_type="call_batch",
            entity_id=batch.id,
            event_type="batch.dispatch_failed",
            actor=ActorType.PROVIDER,
            payload={"error_class": str(exc.error_class), "retryable": exc.retryable},
        )
        session.commit()
        raise

    batch.provider_call_id = handle.provider_call_id
    batch.status = BatchStatus.DISPATCHED
    batch.dispatched_at = _now()

    if not is_live:
        # Store the simulated payload so the worker -- a different
        # process with a different in-memory mock -- can reconcile it.
        from app.adapters.calle.mock import MockCalleProvider

        if isinstance(provider, MockCalleProvider):
            batch.mock_payload = provider.raw_call(handle.provider_call_id)
            batch.mock_events = provider.list_events(handle.provider_call_id)

    # Correlate provider recipient ids back onto our attempts by
    # submission order -- the create response is the first place these
    # ids exist.
    if len(handle.provider_recipient_ids) != len(plan.attempts):
        # A silent zip() truncation here would leave attempts with no
        # provider_recipient_id, and their results would later arrive
        # unattributable. Surface it instead.
        log.warning(
            "provider returned a different recipient count than submitted",
            extra={
                "batch_id": batch.id,
                "submitted": len(plan.attempts),
                "returned": len(handle.provider_recipient_ids),
            },
        )
    for attempt, recipient_id in zip(
        plan.attempts, handle.provider_recipient_ids, strict=False
    ):
        attempt.provider_recipient_id = recipient_id
        attempt.status = CallAttemptStatus.DISPATCHED
        attempt.dispatched_at = _now()

    for rc in recipients:
        record = session.get(ExceptionRecord, rc.exception_id)
        if record is None:
            continue
        repo.transition_exception(
            session,
            record.id,
            expected_version=record.version,
            new_state=ExceptionState.CALLING,
            actor=ActorType.SYSTEM,
            event_type="exception.calling",
            payload={
                "provider_call_id": handle.provider_call_id,
                "attempt_no": rc.attempt_no,
                "is_live": is_live,
                "deduplicated": handle.deduplicated,
            },
        )

    repo.record_audit(
        session,
        entity_type="call_batch",
        entity_id=batch.id,
        event_type="batch.dispatched",
        actor=ActorType.SYSTEM,
        payload={
            "provider_call_id": handle.provider_call_id,
            "recipient_count": len(recipients),
            "idempotency_key": idempotency_key,
            "deduplicated": handle.deduplicated,
            "is_live": is_live,
        },
    )
    session.commit()

    log.info(
        "batch dispatched",
        extra={
            "batch_id": batch.id,
            "provider_call_id": handle.provider_call_id,
            "recipients": len(recipients),
            "is_live": is_live,
            "deduplicated": handle.deduplicated,
        },
    )
    return batch


def resolve_exceptions(
    session: Session,
    exception_ids: list[str],
    *,
    settings: Settings | None = None,
) -> CallBatch | None:
    """Plan and dispatch in one step. The API and scanner entry point."""
    plan = plan_batch(session, exception_ids, settings=settings)
    if not plan:
        session.commit()
        return None
    return dispatch_batch(session, plan, settings=settings)
