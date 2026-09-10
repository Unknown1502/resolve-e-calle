"""Attempt budget and retry backoff.

``docs/prompts/decision-engine.md`` -- "Retry only when":

    the previous action is known to be retryable;
    the attempt budget permits it;
    the retry delay policy permits it;
    the same logical attempt is not already active;
    the exception is not terminal.

All five are checked here. The schedule is a policy setting, never a
model decision (``reliability-and-failure.md`` §8).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import (
    RETRYABLE_ERROR_CLASSES,
    CallAttemptStatus,
    Decision,
    ErrorClass,
    ReasonCode,
)
from app.domain.models import ExceptionSnapshot, PolicyDecision, PolicySettings
from app.domain.state_machine import is_terminal


def check_attempt_budget(
    exception: ExceptionSnapshot,
    settings: PolicySettings,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    """Decide whether another attempt may be dispatched right now."""
    now = now or datetime.now(UTC)
    next_attempt_no = exception.attempt_count + 1
    snapshot = {
        "exception_id": exception.exception_id,
        "attempt_count": exception.attempt_count,
        "next_attempt_no": next_attempt_no,
        "max_attempts": settings.max_attempts,
        "evaluated_at": now.isoformat(),
    }

    if is_terminal(exception.state):
        return PolicyDecision(
            decision=Decision.NOOP,
            reason_code=ReasonCode.ALREADY_TERMINAL,
            reason_text=f"Exception is terminal ({exception.state}); no further attempts.",
            input_snapshot=snapshot,
        )

    # Budget exhausted -> a human takes it, we do not keep dialling.
    if exception.attempt_count >= settings.max_attempts:
        return PolicyDecision(
            decision=Decision.HUMAN_REVIEW,
            reason_code=ReasonCode.ATTEMPT_BUDGET_EXHAUSTED,
            reason_text=(
                f"{exception.attempt_count} of {settings.max_attempts} permitted attempts "
                "have been used; escalating instead of calling again."
            ),
            input_snapshot=snapshot,
        )

    # An attempt is already in flight. Dispatching now would create the
    # duplicate call the whole design exists to prevent.
    if exception.last_attempt_status == CallAttemptStatus.DISPATCHED:
        return PolicyDecision(
            decision=Decision.NOOP,
            reason_code=ReasonCode.RETRY_BACKOFF_NOT_ELAPSED,
            reason_text="An attempt is already dispatched and has not reached a terminal state.",
            input_snapshot=snapshot,
        )

    # Backoff between attempts.
    required_wait = timedelta(minutes=settings.backoff_minutes_for(next_attempt_no))
    if exception.last_attempt_at is not None and required_wait:
        earliest = exception.last_attempt_at + required_wait
        if now < earliest:
            return PolicyDecision(
                decision=Decision.NOOP,
                reason_code=ReasonCode.RETRY_BACKOFF_NOT_ELAPSED,
                reason_text=(
                    f"Attempt {next_attempt_no} is not due until {earliest.isoformat()} "
                    f"({required_wait.total_seconds() / 60:.0f} minute backoff)."
                ),
                input_snapshot={**snapshot, "earliest_next_attempt": earliest.isoformat()},
            )

    return PolicyDecision(
        decision=Decision.PROCEED_WITH_CALL,
        reason_code=ReasonCode.ATTEMPT_BUDGET_AVAILABLE,
        reason_text=(
            f"Attempt {next_attempt_no} of {settings.max_attempts} is within budget "
            "and the backoff has elapsed."
        ),
        input_snapshot=snapshot,
    )


def is_retryable(error_class: ErrorClass | None) -> bool:
    """Only explicitly-retryable classes may drive an automatic retry.

    ``docs/architecture/low-level.md``: "Retry only classes explicitly
    marked retryable." Note what is absent -- ``POLICY`` refusals and
    ``AUTHORIZATION`` failures are never retried, because repeating them
    cannot change the answer.
    """
    return error_class is not None and error_class in RETRYABLE_ERROR_CLASSES
