"""Translate CALL-E payloads into internal domain objects.

This is the only place provider field names appear on the way *in*.
Everything downstream -- policy, state machine, persistence, UI -- sees
:class:`NormalizedCall` and :class:`NormalizedRecipient`.

The mapper is deliberately tolerant about *shape* and strict about
*meaning*: a missing optional field becomes ``None`` rather than raising,
because the provider is explicit that several fields are ``null`` until a
call reaches a terminal state. But an unrecognised status is never
coerced into a familiar one -- it is passed through so the policy engine
can refuse to act on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.models import (
    CompletionConfidence,
    NormalizedAttempt,
    NormalizedCall,
    NormalizedRecipient,
    TranscriptTurn,
)

#: ``WebhookEventType`` from the OpenAPI spec.
WEBHOOK_EVENT_TYPES = frozenset(
    {"call.completed", "call.failed", "call.result_validation_failed"}
)

#: Terminal ``CallStatus`` values.
TERMINAL_CALL_STATUSES = frozenset({"completed", "failed", "canceled"})


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _map_confidence(raw: Any) -> CompletionConfidence | None:
    """Map ``completion_confidence``; ``null`` until the task is terminal."""
    if not isinstance(raw, dict):
        return None
    score = raw.get("score")
    label = raw.get("label")
    if not isinstance(score, int | float) or not isinstance(label, str):
        return None
    return CompletionConfidence(score=float(score), label=label)


def map_transcript_turn(raw: dict[str, Any]) -> TranscriptTurn | None:
    """Map one ``CallTranscriptTurn``; ``None`` when unusable."""
    text = raw.get("text")
    if not isinstance(text, str) or not text:
        return None
    offset = raw.get("offset_seconds")
    return TranscriptTurn(
        # Unknown speakers pass through rather than being guessed at.
        speaker=str(raw.get("speaker") or "unknown"),
        text=text,
        offset_seconds=offset if isinstance(offset, int) else None,
    )


def map_attempt(raw: dict[str, Any]) -> NormalizedAttempt:
    """Map one ``CallTaskAttempt``, including its transcript."""
    turns = raw.get("transcript_turns")
    transcript = (
        tuple(
            turn
            for t in turns
            if isinstance(t, dict) and (turn := map_transcript_turn(t)) is not None
        )
        if isinstance(turns, list)
        else ()
    )
    return NormalizedAttempt(
        provider_attempt_id=str(raw.get("id") or ""),
        status=str(raw.get("status") or "queued"),
        phone=raw.get("phone") if isinstance(raw.get("phone"), str) else None,
        summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        transcript=transcript,
        failure_code=raw.get("failure_code")
        if isinstance(raw.get("failure_code"), str)
        else None,
        failure_message=raw.get("failure_message")
        if isinstance(raw.get("failure_message"), str)
        else None,
    )


def map_recipient(raw: dict[str, Any]) -> NormalizedRecipient:
    """Map one ``CallTaskRecipient``."""
    result = raw.get("structured_result")
    attempts = raw.get("attempts")
    return NormalizedRecipient(
        provider_recipient_id=str(raw.get("id") or ""),
        phones=tuple(str(p) for p in (raw.get("phones") or [])),
        # Passed through verbatim. An unknown status must not be
        # silently normalised into "completed".
        status=str(raw.get("status") or "pending"),
        raw_structured_result=result if isinstance(result, dict) else None,
        summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        attempts=tuple(
            map_attempt(a) for a in attempts if isinstance(a, dict)
        )
        if isinstance(attempts, list)
        else (),
    )


def map_call(raw: dict[str, Any]) -> NormalizedCall:
    """Map a ``CallTask`` object from create, retrieve, or a webhook body.

    The same shape is returned by all three -- the spec describes the
    webhook's ``data`` as "the same stable ``call_task`` object returned
    by the calls API" -- so one mapper covers every path.
    """
    task_result = raw.get("structured_result")
    evidence = raw.get("evidence")
    metadata = raw.get("metadata")

    return NormalizedCall(
        provider_call_id=str(raw.get("id") or ""),
        status=str(raw.get("status") or "queued"),
        recipients=tuple(
            map_recipient(r) for r in (raw.get("recipients") or []) if isinstance(r, dict)
        ),
        raw_structured_result=task_result if isinstance(task_result, dict) else None,
        summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        # Tri-state on purpose: None means "not yet judged", which is
        # different from False. The evidence policy treats both as
        # insufficient, but the audit trail keeps them distinguishable.
        task_completed=raw.get("task_completed")
        if isinstance(raw.get("task_completed"), bool)
        else None,
        completion_confidence=_map_confidence(raw.get("completion_confidence")),
        evidence=tuple(str(e) for e in evidence) if isinstance(evidence, list) else (),
        metadata=metadata if isinstance(metadata, dict) else {},
        # Persisted for support and display only. Never branched on:
        # the provider publishes no enum for it.
        failure_code=raw.get("failure_code")
        if isinstance(raw.get("failure_code"), str)
        else None,
        failure_message=raw.get("failure_message")
        if isinstance(raw.get("failure_message"), str)
        else None,
        created_at=_parse_dt(raw.get("created_at")),
        completed_at=_parse_dt(raw.get("completed_at")),
    )


def extract_webhook_call(body: dict[str, Any]) -> tuple[str, str, NormalizedCall]:
    """Pull ``(event_id, event_type, call)`` out of a webhook body.

    Raises:
        ValueError: the body is not a recognisable CALL-E webhook. The
            receiver turns this into a 400 without touching any state.
    """
    event_id = body.get("id")
    event_type = body.get("type")
    data = body.get("data")

    if not isinstance(event_id, str) or not event_id:
        raise ValueError("webhook body has no event id")
    if not isinstance(event_type, str) or event_type not in WEBHOOK_EVENT_TYPES:
        raise ValueError(f"unrecognised webhook event type: {event_type!r}")
    if not isinstance(data, dict):
        raise ValueError("webhook body has no call data object")

    call = map_call(data)
    if not call.provider_call_id:
        raise ValueError("webhook call data has no call id")

    return event_id, event_type, call
