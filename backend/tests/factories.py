"""Builders for policy-engine tests.

Every builder returns a *valid* object by default, so each test changes
exactly the one thing it is about. That keeps the tests readable as
statements of policy rather than as object construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import CallAttemptStatus, ExceptionState
from app.domain.models import (
    CompletionConfidence,
    ExceptionSnapshot,
    NormalizedAttempt,
    NormalizedCall,
    NormalizedRecipient,
    PolicySettings,
    TranscriptTurn,
)

NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
OVERDUE_BY_26H = NOW - timedelta(hours=26)


def make_settings(**overrides: Any) -> PolicySettings:
    return PolicySettings(**overrides)


def make_exception(
    *,
    state: ExceptionState = ExceptionState.CALLING,
    attempt_count: int = 1,
    ack_due_at: datetime | None = None,
    **overrides: Any,
) -> ExceptionSnapshot:
    defaults: dict[str, Any] = {
        "exception_id": "exc_4821",
        "po_number": "PO-4821",
        "supplier_name": "Acme Components",
        "recipient_name": "Jordan",
        "recipient_phone_e164": "+15550001111",
        "ack_due_at": ack_due_at or OVERDUE_BY_26H,
        "expected_ship_date": None,
        "state": state,
        "version": 1,
        "attempt_count": attempt_count,
        "last_attempt_at": NOW - timedelta(hours=2),
        "last_attempt_status": CallAttemptStatus.COMPLETED,
        "recipient_blocklisted": False,
    }
    return ExceptionSnapshot(**{**defaults, **overrides})


def make_result(
    *,
    received: str = "yes",
    po_status: str = "on_time",
    ship_date: str = "2026-09-11",
    blocker: str = "none",
    needs_human: str = "no",
    spoke_with: str = "intended_contact",
    escalation_reason: str = "none",
) -> dict[str, str]:
    """A raw *wire-shaped* recipient result, schema v2.

    Note ``po_status``: CALL-E reserves ``status`` as a recipient field
    name, so the schema we submit renames it. See
    ``docs/provider-truth.md`` §3.

    ``spoke_with`` and ``escalation_reason`` are v2 additions: the
    identity of who answered, and a differentiated reason a human is
    needed (as opposed to a single ``needs_human`` flag that could not
    tell a refusal from a commercial ask from a routine request for a
    person). Defaults describe a clean, identified, routine call so most
    tests only need to override the field they are actually about.
    """
    return {
        "received": received,
        "po_status": po_status,
        "ship_date": ship_date,
        "blocker": blocker,
        "needs_human": needs_human,
        "spoke_with": spoke_with,
        "escalation_reason": escalation_reason,
    }


def make_recipient(
    *,
    status: str = "completed",
    result: dict[str, Any] | None | str = "default",
    provider_recipient_id: str = "rcp_1",
    phones: tuple[str, ...] = ("+15550001111",),
    summary: str | None = "Supplier confirmed receipt.",
    transcript_line: str = "Yes, we received it.",
) -> NormalizedRecipient:
    raw = make_result() if result == "default" else result
    return NormalizedRecipient(
        provider_recipient_id=provider_recipient_id,
        phones=phones,
        status=status,
        raw_structured_result=raw,  # type: ignore[arg-type]
        summary=summary,
        # The conversation hangs off the attempt, not the recipient.
        attempts=(
            NormalizedAttempt(
                provider_attempt_id=f"{provider_recipient_id}_att1",
                status="completed" if status == "completed" else "failed",
                phone=phones[0] if phones else None,
                summary=summary,
                transcript=(
                    TranscriptTurn(speaker="bot", text="Calling about the order.", offset_seconds=0),
                    TranscriptTurn(speaker="user", text=transcript_line, offset_seconds=5),
                )
                if transcript_line
                else (),
            ),
        ),
    )


def make_call(
    *,
    status: str = "completed",
    recipients: tuple[NormalizedRecipient, ...] | None = None,
    task_completed: bool | None = True,
    confidence: float | None = 0.92,
    confidence_label: str = "high",
    failure_code: str | None = None,
    **overrides: Any,
) -> NormalizedCall:
    conf = (
        CompletionConfidence(score=confidence, label=confidence_label)
        if confidence is not None
        else None
    )
    defaults: dict[str, Any] = {
        "provider_call_id": "call_abc123",
        "status": status,
        "recipients": recipients if recipients is not None else (make_recipient(),),
        "raw_structured_result": {"resolved_count": 1},
        "summary": "Called Acme Components about PO-4821.",
        "task_completed": task_completed,
        "completion_confidence": conf,
        "evidence": ("Supplier said the order shipped Friday.",),
        "metadata": {"exception_id": "exc_4821", "attempt_no": 1},
        "failure_code": failure_code,
        "failure_message": None,
        "created_at": NOW - timedelta(minutes=5),
        "completed_at": NOW,
    }
    return NormalizedCall(**{**defaults, **overrides})
