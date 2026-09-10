"""May Resolve-E place this call at all?

This is the first gate and it fails closed. Every rejection rule is
listed in ``docs/prompts/call-task-template.md`` under "Prompt
construction rules"; this module is that list, executed.

Order matters. Cheap, certain refusals (already terminal, no phone) run
before anything that needs a clock, so that a decision is never
dependent on when it was evaluated more than it has to be.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import Decision, ExceptionState, ReasonCode
from app.domain.models import (
    ExceptionSnapshot,
    PolicyDecision,
    PolicySettings,
    is_valid_e164,
)
from app.domain.state_machine import is_terminal


def check_call_eligibility(
    exception: ExceptionSnapshot,
    settings: PolicySettings,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    """Decide whether an outbound call to this exception's contact is permitted.

    Returns a :class:`PolicyDecision` of either
    :attr:`Decision.PROCEED_WITH_CALL` or :attr:`Decision.DO_NOT_CALL`.
    It never raises for a *policy* refusal -- a refusal is a decision,
    and decisions are audited.
    """
    now = now or datetime.now(UTC)
    snapshot = {
        "exception_id": exception.exception_id,
        "po_number": exception.po_number,
        "state": str(exception.state),
        "attempt_count": exception.attempt_count,
        "evaluated_at": now.isoformat(),
    }

    def refuse(code: ReasonCode, text: str) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.DO_NOT_CALL,
            reason_code=code,
            reason_text=text,
            input_snapshot=snapshot,
        )

    # 1. Already finished. Nothing to chase.
    if is_terminal(exception.state):
        return refuse(
            ReasonCode.ALREADY_TERMINAL,
            f"Exception is already in terminal state {exception.state}.",
        )

    # 2. Reliability rule 15: never dial an unvalidated target.
    if exception.recipient_blocklisted:
        return refuse(
            ReasonCode.RECIPIENT_BLOCKLISTED,
            "Recipient is blocklisted; outbound calling is prohibited.",
        )

    if not exception.recipient_phone_e164:
        return refuse(
            ReasonCode.NO_AUTHORIZED_PHONE,
            "No authorized phone number is recorded for this supplier contact.",
        )

    if not is_valid_e164(exception.recipient_phone_e164):
        # Caught here rather than at the provider so the failure is
        # auditable and carries a reason code. Same regex the provider
        # enforces -- see docs/provider-truth.md §2.
        return refuse(
            ReasonCode.PHONE_NOT_E164,
            "Recorded phone number is not valid E.164 and will not be dialled.",
        )

    # 3. Not yet overdue -- the exception is not real yet.
    if exception.ack_due_at > now:
        return refuse(
            ReasonCode.NOT_YET_OVERDUE,
            (
                f"Acknowledgement is not due until {exception.ack_due_at.isoformat()}; "
                "calling early is out of policy."
            ),
        )

    # 4. Quiet hours. Off by default so a judge can run the demo at any
    #    hour; on in any deployment that dials real suppliers.
    if settings.enforce_quiet_hours and _in_quiet_hours(now, settings):
        return refuse(
            ReasonCode.QUIET_HOURS,
            (
                f"Local time {now.strftime('%H:%M')} falls inside quiet hours "
                f"({settings.quiet_hours_start:02d}:00-{settings.quiet_hours_end:02d}:00)."
            ),
        )

    return PolicyDecision(
        decision=Decision.PROCEED_WITH_CALL,
        reason_code=ReasonCode.ELIGIBLE_FOR_CALL,
        reason_text=(
            f"PO {exception.po_number} is past its acknowledgement deadline and the "
            "supplier contact is authorized and callable."
        ),
        input_snapshot=snapshot,
    )


def _in_quiet_hours(now: datetime, settings: PolicySettings) -> bool:
    """Quiet hours wrap midnight, so this is not a simple range test."""
    hour = now.hour
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def stale_exception_states() -> frozenset[ExceptionState]:
    """States the scanner may pick up and drive toward a call."""
    return frozenset({ExceptionState.OPEN, ExceptionState.RETRY_PENDING})

#: Refusals that will resolve themselves as time passes. An exception
#: refused for one of these must stay in the queue and be retried later,
#: NOT escalated -- putting "we called too early" on a human's queue is a
#: false alarm, and false alarms are how an operator learns to ignore
#: the queue.
TEMPORAL_REFUSALS: frozenset[ReasonCode] = frozenset(
    {ReasonCode.NOT_YET_OVERDUE, ReasonCode.QUIET_HOURS}
)


def refusal_needs_a_human(reason_code: ReasonCode) -> bool:
    """True when a refusal cannot fix itself and someone must intervene.

    A missing or malformed phone number, or a blocklisted recipient, is
    a data problem that will never improve on its own.
    """
    return reason_code not in TEMPORAL_REFUSALS and reason_code is not ReasonCode.ALREADY_TERMINAL

