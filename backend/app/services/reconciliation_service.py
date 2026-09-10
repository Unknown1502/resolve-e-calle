"""Authoritative state recovery.

``docs/architecture/high-level.md``:

    The webhook is not itself the decision engine. The webhook only
    reports "a CALL-E task reached terminal state X". Resolve-E fetches
    and reconciles authoritative call state as needed.

So this module -- not the webhook receiver -- is where results actually
get decided. Two paths lead here:

1. a webhook arrived and enqueued reconciliation for that call;
2. nothing arrived, and a batch has been ``DISPATCHED`` longer than
   ``reconciliation_after_seconds``.

Both call :func:`reconcile_call`, which asks CALL-E for the truth. That
means a lost webhook is a delay, never a stuck workflow.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.adapters.calle.mock import MockCalleProvider
from app.config import Settings, get_settings
from app.db import repositories as repo
from app.db.models import ExceptionRecord
from app.domain.enums import ActorType, Decision, ExceptionState
from app.domain.errors import InvalidTransition, ResolveEError
from app.services.provider import get_mock_provider
from app.services.result_processor import process_terminal_call

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class _StoredPayloadProvider:
    """Replays a simulated call from the payload stored on its batch.

    Reconciliation must work from any process. A live batch is fetched
    from CALL-E; a simulated one is replayed from ``mock_payload``,
    which the dispatching process persisted. Both come back through
    ``map_call``, so the reconciliation code path is identical.
    """

    def __init__(self, payload: dict, events: list[dict] | None = None) -> None:
        self._payload = payload
        self._events = events or []

    def get_call(self, call_id: str):
        from app.adapters.calle.mapper import map_call

        return map_call(self._payload)

    def list_events(self, call_id: str) -> list[dict]:
        # Replayed from the snapshot the dispatching process stored, so
        # the audit trail looks the same whichever process reconciles.
        return list(self._events)


def _collect_provider_events(
    session: Session, batch, provider, provider_call_id: str
) -> int:
    """Pull CALL-E's developer events into our audit trail.

    ``GET /v1/calls/{id}/events`` is the provider's own view of what
    happened on the call. Folding it into the same timeline as our state
    transitions is what lets an operator answer "the agent says the
    supplier confirmed -- what did CALL-E actually see?" without leaving
    the page.

    Diagnostics are best-effort by definition: a failure here is logged
    and swallowed, because losing the event stream must never prevent a
    call from being resolved.
    """
    try:
        events = provider.list_events(provider_call_id)
    except Exception as exc:  # noqa: BLE001 - diagnostics are optional
        log.info(
            "could not fetch provider events",
            extra={"provider_call_id": provider_call_id, "error": str(exc)},
        )
        return 0

    recorded = 0
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        # Provider events are immutable, so the event id is a natural
        # dedupe key across repeated reconciliations.
        if repo.audit_event_exists(session, batch.id, str(event_id)):
            continue
        repo.record_audit(
            session,
            entity_type="call_batch",
            entity_id=batch.id,
            event_type=f"calle.{event.get('type', 'event')}",
            actor=ActorType.PROVIDER,
            payload={
                "provider_event_id": event_id,
                "level": event.get("level"),
                "status": event.get("status"),
                "message": event.get("message"),
                "details": event.get("details") or {},
            },
        )
        recorded += 1
    return recorded


def _provider_for(batch, settings: Settings):
    """The provider that owns this batch.

    A batch dispatched to the mock must be reconciled against the mock,
    even if live calling has since been switched on -- otherwise we would
    ask CALL-E about a call id it has never seen.
    """
    if not batch.is_live:
        # In-memory first: it is live state and can advance (a deferred
        # call reaching terminal, say). The stored payload is a frozen
        # dispatch-time snapshot, correct only as a cross-process
        # fallback -- preferring it would replay stale state forever.
        mock = get_mock_provider()
        if mock.knows(batch.provider_call_id or ""):
            return mock
        if batch.mock_payload:
            return _StoredPayloadProvider(batch.mock_payload, batch.mock_events)
        return mock
    from app.services.provider import live_provider

    return live_provider(settings)


def reconcile_call(
    session: Session,
    provider_call_id: str,
    *,
    settings: Settings | None = None,
) -> list[tuple[str, Decision]]:
    """Fetch authoritative state for a call and apply it.

    This is the only function that decides a result. The webhook path
    and the polling path both funnel through it, which is why a
    duplicate webhook is harmless: the second delivery re-fetches the
    same terminal state and every transition it would make is already
    made, so the state machine refuses the no-op.
    """
    settings = settings or get_settings()

    batch = repo.batch_by_provider_call_id(session, provider_call_id)
    if batch is None:
        log.warning("reconcile: unknown call", extra={"provider_call_id": provider_call_id})
        return []

    provider = _provider_for(batch, settings)

    try:
        call = provider.get_call(provider_call_id)
    except ResolveEError as exc:
        repo.record_audit(
            session,
            entity_type="call_batch",
            entity_id=batch.id,
            event_type="batch.reconcile_failed",
            actor=ActorType.PROVIDER,
            payload={"error_class": str(exc.error_class), "retryable": exc.retryable},
        )
        log.warning(
            "reconcile failed",
            extra={"provider_call_id": provider_call_id, "error_class": str(exc.error_class)},
        )
        raise

    recorded = _collect_provider_events(session, batch, provider, provider_call_id)

    repo.record_audit(
        session,
        entity_type="call_batch",
        entity_id=batch.id,
        event_type="batch.reconciled",
        actor=ActorType.SYSTEM,
        payload={
            "provider_call_id": provider_call_id,
            "provider_status": call.status,
            "is_terminal": call.is_terminal,
            "provider_events_recorded": recorded,
        },
    )

    if not batch.is_live and isinstance(provider, MockCalleProvider):
        # Refresh the cross-process snapshot with what we just saw.
        # Wrapped for the same reason the fetch above is: the event
        # stream is diagnostics, and diagnostics must never be able to
        # fail a reconciliation that already has authoritative state.
        batch.mock_payload = provider.raw_call(provider_call_id)
        try:
            batch.mock_events = provider.list_events(provider_call_id)
        except Exception as exc:  # noqa: BLE001 - diagnostics are optional
            log.info(
                "could not refresh simulated event snapshot",
                extra={"provider_call_id": provider_call_id, "error": str(exc)},
            )

    if not call.is_terminal:
        # Provider says the call is still running. Do not invent a
        # result; go back to waiting.
        _return_to_calling(session, batch)
        session.flush()
        return []

    # reconciliation_available=False: we have just fetched authoritative
    # state. If the result is still missing, fetching again cannot help,
    # so the engine moves on to retry or escalation.
    return process_terminal_call(
        session, call, settings=settings, reconciliation_available=False
    )


def _return_to_calling(session: Session, batch) -> None:
    for attempt in repo.attempts_for_batch(session, batch.id):
        record = session.get(ExceptionRecord, attempt.run.exception_id)
        if record is None or ExceptionState(record.state) != ExceptionState.RECONCILING:
            continue
        # Benign: the exception moved on under us.
        with contextlib.suppress(InvalidTransition):
            repo.transition_exception(
                session,
                record.id,
                expected_version=record.version,
                new_state=ExceptionState.CALLING,
                actor=ActorType.SYSTEM,
                event_type="exception.still_calling",
                payload={"provider_call_id": batch.provider_call_id},
            )


def mark_reconciling(session: Session, batch) -> None:
    """Move covered exceptions into RECONCILING so the UI shows it."""
    for attempt in repo.attempts_for_batch(session, batch.id):
        record = session.get(ExceptionRecord, attempt.run.exception_id)
        if record is None or ExceptionState(record.state) != ExceptionState.CALLING:
            continue
        with contextlib.suppress(InvalidTransition):
            repo.transition_exception(
                session,
                record.id,
                expected_version=record.version,
                new_state=ExceptionState.RECONCILING,
                actor=ActorType.SYSTEM,
                event_type="exception.reconciling",
                payload={"provider_call_id": batch.provider_call_id},
            )


def sweep_stale_calls(
    session: Session, *, settings: Settings | None = None
) -> list[str]:
    """Reconcile every batch that has been waiting too long for a webhook.

    This is the repair path for a webhook that was never delivered. It
    is also what makes the system correct without webhooks at all --
    useful for a local demo where CALL-E cannot reach your laptop.
    """
    settings = settings or get_settings()
    cutoff = _now() - timedelta(seconds=settings.reconciliation_after_seconds)
    reconciled: list[str] = []

    for batch in repo.stale_calling_batches(session, older_than=cutoff):
        if not batch.provider_call_id:
            continue
        mark_reconciling(session, batch)
        session.flush()
        try:
            reconcile_call(session, batch.provider_call_id, settings=settings)
            reconciled.append(batch.provider_call_id)
        except ResolveEError:
            # Already audited. Try again on the next sweep.
            continue

    return reconciled
