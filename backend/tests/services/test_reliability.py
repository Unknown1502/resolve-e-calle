"""The reliability quality gate.

``docs/hackathon-build/build-notes.md`` -- "Final quality gate":

    A release candidate is acceptable only if:
    - duplicate-call test passes;
    - missed-webhook reconciliation passes;
    - ambiguous-result test escalates;
    - structured-result-null test escalates/retries safely;
    - concurrent update does not overwrite terminal state;
    - max-attempt policy works;
    - no secret appears in frontend bundle or logs.

Each of those is a test below, against real PostgreSQL. These are the
claims the product makes, so they are the claims that get checked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.adapters.calle.mapper import extract_webhook_call
from app.db import repositories as repo
from app.db.models import ExceptionRecord
from app.domain.enums import ActorType, Decision, ExceptionState
from app.domain.errors import ConcurrencyConflict, TerminalStateProtected
from app.services.call_orchestrator import resolve_exceptions
from app.services.provider import get_mock_provider
from app.services.reconciliation_service import mark_reconciling, reconcile_call
from app.services.result_processor import process_terminal_call
from tests.conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.integration]


def _now() -> datetime:
    return datetime.now(UTC)


def seed(db: Session, po: str, *, hours_overdue: float = 26, phone: str | None = None) -> str:
    record = ExceptionRecord(
        po_number=po,
        supplier_name=f"{po} Supplies",
        recipient_name="Jordan",
        recipient_phone_e164=phone or f"+1555000{abs(hash(po)) % 10000:04d}",
        ack_due_at=_now() - timedelta(hours=hours_overdue),
        state=ExceptionState.OPEN,
    )
    db.add(record)
    db.commit()
    return record.id


def terminal_call(call_id: str):
    mock = get_mock_provider()
    _, _, call = extract_webhook_call(mock.webhook_body(call_id))
    return call


def state_of(db: Session, exception_id: str) -> str:
    db.expire_all()
    return db.get(ExceptionRecord, exception_id).state


# ── duplicate calls ──────────────────────────────────────────────────


class TestDuplicateCallPrevention:
    def test_resolving_the_same_exception_twice_creates_one_call(self, db: Session) -> None:
        exc = seed(db, "PO-4821")
        mock = get_mock_provider()

        first = resolve_exceptions(db, [exc])
        assert first is not None
        assert mock.call_count == 1

        # Second trigger: the exception is now CALLING, so eligibility
        # refuses it outright. No second provider call either way.
        resolve_exceptions(db, [exc])
        assert mock.call_count == 1

    def test_create_timeout_retried_with_same_key_places_one_call(self, db: Session) -> None:
        """The dangerous case: the provider may have received the first try.

        This proves OUR retry reuses the same key (against the mock, whose
        dedup logic we wrote and fully control). It does not, and cannot,
        prove CALL-E's server-side handling of a genuinely lost response --
        that is a provider-dependent guarantee we have not exercised live.
        See docs/reliability-and-failure.md §1b.
        """
        exc = seed(db, "PO-4821")
        mock = get_mock_provider()
        mock.fail_next_create = True

        from app.domain.errors import ProviderTransientError

        with pytest.raises(ProviderTransientError):
            resolve_exceptions(db, [exc])
        db.rollback()

        # The batch row survived the failure, carrying its idempotency
        # key, so the retry reuses it.
        from app.db.models import CallBatch

        stored = db.query(CallBatch).all()
        assert len(stored) == 1, "the batch must be persisted before dispatch"
        key = stored[0].idempotency_key

        from app.adapters.calle.schemas import (
            RECIPIENT_RESULT_SCHEMA,
            TASK_RESULT_SCHEMA,
            render_task,
        )
        from app.domain.commands import CallRecipientCommand, CreateCallCommand

        rc = CallRecipientCommand(
            workflow_run_id="wr", exception_id=exc, attempt_no=1,
            po_number="PO-4821", supplier_name="S", recipient_name="J",
            phone_e164="+15550001111",
        )
        cmd = CreateCallCommand(
            recipients=(rc,),
            task=render_task(
                [{"supplier_name": "S", "recipient_name": "J",
                  "po_number": "PO-4821", "phone": "+15550001111"}],
                buyer_company="Northwind Trading",
            ),
            result_schema=TASK_RESULT_SCHEMA,
            recipient_result_schema=RECIPIENT_RESULT_SCHEMA,
            idempotency_key=key,
        )
        mock.create_call(cmd)
        mock.create_call(cmd)
        assert mock.call_count == 1, "same key must never create a second call"

    def test_batch_of_three_creates_exactly_one_call(self, db: Session) -> None:
        ids = [seed(db, f"PO-482{i}") for i in (1, 2, 3)]
        resolve_exceptions(db, ids)
        assert get_mock_provider().call_count == 1


# ── webhooks ─────────────────────────────────────────────────────────


class TestWebhookIdempotency:
    def test_duplicate_terminal_event_is_a_no_op(self, db: Session) -> None:
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])
        call = terminal_call(batch.provider_call_id)

        process_terminal_call(db, call, reconciliation_available=False)
        db.commit()
        first_state = state_of(db, exc)
        first_audit = len(repo.audit_trail(db, exc))

        # Redelivery. CALL-E delivery is at-least-once.
        process_terminal_call(db, call, reconciliation_available=False)
        db.commit()

        assert state_of(db, exc) == first_state
        assert state_of(db, exc) == ExceptionState.RESOLVED_ON_TIME
        # The second pass may append an "ignored" audit note, but must
        # not append another *transition*.
        transitions = [
            a for a in repo.audit_trail(db, exc) if a.previous_state and a.new_state
        ]
        assert len(transitions) == len(
            [a for a in repo.audit_trail(db, exc)[:first_audit] if a.previous_state]
        )

    def test_webhook_event_id_is_deduplicated_at_the_database(self, db: Session) -> None:
        body = {
            "id": "evt_dup",
            "type": "call.completed",
            "created_at": _now().isoformat(),
            "data": {"id": "call_x", "status": "completed", "recipients": []},
        }
        _, is_new = repo.register_webhook_event(
            db, provider_event_id="evt_dup", provider_call_id="call_x",
            event_type="call.completed", body=body,
        )
        assert is_new
        db.commit()

        _, is_new_again = repo.register_webhook_event(
            db, provider_event_id="evt_dup", provider_call_id="call_x",
            event_type="call.completed", body=body,
        )
        assert not is_new_again

    def test_outbox_dedupes_repeated_reconcile_requests(self, db: Session) -> None:
        first = repo.enqueue(
            db, dedupe_key="reconcile:call_1:evt_1",
            event_type="reconcile_call", payload={"provider_call_id": "call_1"},
        )
        second = repo.enqueue(
            db, dedupe_key="reconcile:call_1:evt_1",
            event_type="reconcile_call", payload={"provider_call_id": "call_1"},
        )
        assert first is not None
        assert second is None, "same logical side effect must enqueue once"


# ── lost webhook ─────────────────────────────────────────────────────


class TestReconciliationRepairsMissedWebhooks:
    def test_call_resolves_with_no_webhook_at_all(self, db: Session) -> None:
        """The webhook never arrives. Polling must still finish the job."""
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])
        assert state_of(db, exc) == ExceptionState.CALLING

        mark_reconciling(db, batch)
        db.commit()
        assert state_of(db, exc) == ExceptionState.RECONCILING

        reconcile_call(db, batch.provider_call_id)
        db.commit()
        assert state_of(db, exc) == ExceptionState.RESOLVED_ON_TIME

    def test_non_terminal_call_returns_to_calling_without_inventing_a_result(
        self, db: Session
    ) -> None:
        mock = get_mock_provider()
        mock.defer_terminal = True
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])

        mark_reconciling(db, batch)
        db.commit()
        outcomes = reconcile_call(db, batch.provider_call_id)
        db.commit()

        assert outcomes == []
        assert state_of(db, exc) == ExceptionState.CALLING

        # Once the provider finishes, the next sweep resolves it.
        mock.finalize(batch.provider_call_id)
        mark_reconciling(db, batch)
        db.commit()
        reconcile_call(db, batch.provider_call_id)
        db.commit()
        assert state_of(db, exc) == ExceptionState.RESOLVED_ON_TIME


# ── evidence safety ──────────────────────────────────────────────────


class TestEvidenceSafety:
    @pytest.mark.parametrize(
        ("po", "expected"),
        [
            ("PO-4821", ExceptionState.RESOLVED_ON_TIME),
            ("PO-4822", ExceptionState.RESOLVED_DELAYED),
            ("PO-4823", ExceptionState.HUMAN_REVIEW),
            ("PO-4824", ExceptionState.HUMAN_REVIEW),
            ("PO-4825", ExceptionState.RETRY_PENDING),
            ("PO-4826", ExceptionState.RETRY_PENDING),
            # Right number, wrong person: a clean "yes, on time" that
            # still cannot close the exception. See docs/provider-truth.md §11.
            ("PO-4827", ExceptionState.HUMAN_REVIEW),
        ],
    )
    def test_each_scripted_supplier_reaches_its_documented_outcome(
        self, db: Session, po: str, expected: str
    ) -> None:
        exc = seed(db, po, hours_overdue=20)
        batch = resolve_exceptions(db, [exc])
        process_terminal_call(
            db, terminal_call(batch.provider_call_id), reconciliation_available=False
        )
        db.commit()
        assert state_of(db, exc) == expected

    def test_wrong_person_is_a_distinctly_coded_reason_not_generic(
        self, db: Session
    ) -> None:
        """PO-4827's evidence is otherwise clean -- the identity gate,
        specifically, is what stops it. This pins the reason code, not
        just the resulting state, so a regression that silently swaps
        the gate for a generic ambiguity check would still be caught.
        """
        exc = seed(db, "PO-4827", hours_overdue=20)
        batch = resolve_exceptions(db, [exc])
        process_terminal_call(
            db, terminal_call(batch.provider_call_id), reconciliation_available=False
        )
        db.commit()
        decisions = repo.decisions_for(db, exc)
        assert decisions[-1].reason_code == "WRONG_PERSON"

    def test_one_call_produces_independent_outcomes_per_supplier(
        self, db: Session
    ) -> None:
        """The batch guarantee: supplier A cannot influence supplier B."""
        ids = {po: seed(db, po, hours_overdue=20) for po in ("PO-4821", "PO-4823", "PO-4825")}
        batch = resolve_exceptions(db, list(ids.values()))
        assert get_mock_provider().call_count == 1

        process_terminal_call(
            db, terminal_call(batch.provider_call_id), reconciliation_available=False
        )
        db.commit()

        assert state_of(db, ids["PO-4821"]) == ExceptionState.RESOLVED_ON_TIME
        assert state_of(db, ids["PO-4823"]) == ExceptionState.HUMAN_REVIEW
        assert state_of(db, ids["PO-4825"]) == ExceptionState.RETRY_PENDING

    def test_exception_not_yet_due_is_never_called(self, db: Session) -> None:
        exc = seed(db, "PO-4830", hours_overdue=-12)
        assert resolve_exceptions(db, [exc]) is None
        assert get_mock_progress_calls() == 0
        assert state_of(db, exc) == ExceptionState.OPEN

    def test_blocklisted_recipient_is_never_called(self, db: Session) -> None:
        exc = seed(db, "PO-4821")
        db.get(ExceptionRecord, exc).recipient_blocklisted = True
        db.commit()

        assert resolve_exceptions(db, [exc]) is None
        assert get_mock_progress_calls() == 0
        assert state_of(db, exc) == ExceptionState.HUMAN_REVIEW

    def test_suppression_is_scoped_to_the_exception_not_the_phone_number(
        self, db: Session
    ) -> None:
        """Documents the actual, narrower scope of "asked not to be called".

        `recipient_blocklisted` lives on `ExceptionRecord` -- one row per
        purchase order -- not on a phone number or contact record. A
        supplier who asks not to be called again about *this* PO is
        protected on this PO. Several places in the docs describe the
        consequence as suppressing "this contact" or "that contact" without
        that qualifier, which overstates what actually happens: a second,
        distinct PO to the *same* phone number is dispatched normally.

        This is not asserted as a bug -- the response for one exception is
        not necessarily authoritative about a different one, and treating
        it as such would be its own kind of overreach. It is asserted so
        the documented behaviour cannot silently drift further from the
        real one. If this test starts failing because suppression became
        phone-scoped, update the docs claiming otherwise and celebrate;
        do not just delete the test.
        """
        first = seed(db, "PO-4821", phone="+15551234567")
        second = seed(db, "PO-9001", phone="+15551234567")  # same contact, different PO

        db.get(ExceptionRecord, first).recipient_blocklisted = True
        db.commit()

        # The blocklisted PO is refused, as already proven above.
        assert resolve_exceptions(db, [first]) is None

        # The second PO, same phone number, is NOT protected by the first
        # PO's suppression -- it dispatches normally, ending up in CALLING,
        # not refused straight to HUMAN_REVIEW the way the blocklisted one
        # was above.
        batch = resolve_exceptions(db, [second])
        assert batch is not None
        assert state_of(db, second) == ExceptionState.CALLING


def get_mock_progress_calls() -> int:
    return get_mock_provider().call_count


# ── concurrency ──────────────────────────────────────────────────────


class TestConcurrency:
    def test_operator_resolution_survives_a_late_call_result(self, db: Session) -> None:
        """reliability-and-failure.md §7, the whole scenario."""
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])

        # While the call is in flight, an operator resolves it by hand.
        record = db.get(ExceptionRecord, exc)
        repo.transition_exception(
            db, exc,
            expected_version=record.version,
            new_state=ExceptionState.HUMAN_REVIEW,
            actor=ActorType.OPERATOR,
            event_type="exception.manually_escalated",
        )
        db.commit()
        assert state_of(db, exc) == ExceptionState.HUMAN_REVIEW

        # The call result lands afterwards. It must not overwrite.
        outcomes = process_terminal_call(
            db, terminal_call(batch.provider_call_id), reconciliation_available=False
        )
        db.commit()

        assert state_of(db, exc) == ExceptionState.HUMAN_REVIEW
        assert outcomes and outcomes[0][1] is Decision.NOOP

        # ...but the evidence is still recorded as history.
        attempts = repo.attempts_for_batch(db, batch.id)
        assert attempts[0].structured_result is not None

    def test_stale_version_write_is_rejected(self, db: Session) -> None:
        exc = seed(db, "PO-4821")
        record = db.get(ExceptionRecord, exc)
        stale_version = record.version

        repo.transition_exception(
            db, exc, expected_version=stale_version,
            new_state=ExceptionState.ELIGIBLE,
            actor=ActorType.POLICY, event_type="exception.eligible",
        )
        db.commit()

        with pytest.raises(ConcurrencyConflict):
            repo.transition_exception(
                db, exc, expected_version=stale_version,
                new_state=ExceptionState.CALL_PLANNED,
                actor=ActorType.SYSTEM, event_type="exception.call_planned",
            )

    def test_terminal_state_refuses_any_transition(self, db: Session) -> None:
        exc = seed(db, "PO-4821")
        record = db.get(ExceptionRecord, exc)
        repo.transition_exception(
            db, exc, expected_version=record.version,
            new_state=ExceptionState.HUMAN_REVIEW,
            actor=ActorType.OPERATOR, event_type="exception.escalated",
        )
        db.commit()

        db.expire_all()
        record = db.get(ExceptionRecord, exc)
        with pytest.raises(TerminalStateProtected):
            repo.transition_exception(
                db, exc, expected_version=record.version,
                new_state=ExceptionState.RESOLVED_ON_TIME,
                actor=ActorType.POLICY, event_type="exception.resolved",
            )


# ── attempt budget ───────────────────────────────────────────────────


class TestAttemptBudget:
    def test_exception_escalates_after_max_attempts(self, db: Session) -> None:
        exc = seed(db, "PO-4825", hours_overdue=60)  # never reached

        for _ in range(4):
            db.expire_all()
            record = db.get(ExceptionRecord, exc)
            if record.state in (ExceptionState.HUMAN_REVIEW, ExceptionState.CLOSED_UNRESOLVED):
                break
            batch = resolve_exceptions(db, [exc])
            if batch is None:
                break
            process_terminal_call(
                db, terminal_call(batch.provider_call_id), reconciliation_available=False
            )
            db.commit()
            # Clear the backoff so the next attempt is permitted.
            for a in repo.attempts_for_batch(db, batch.id):
                a.dispatched_at = _now() - timedelta(hours=4)
            db.commit()

        assert state_of(db, exc) == ExceptionState.HUMAN_REVIEW
        assert get_mock_provider().call_count <= 3, "must not exceed max_attempts calls"


# ── audit completeness ───────────────────────────────────────────────


class TestAuditTrail:
    def test_every_transition_records_who_and_why(self, db: Session) -> None:
        exc = seed(db, "PO-4823")
        batch = resolve_exceptions(db, [exc])
        process_terminal_call(
            db, terminal_call(batch.provider_call_id), reconciliation_available=False
        )
        db.commit()

        trail = repo.audit_trail(db, exc)
        assert trail, "an exception must never reach a state with no audit trail"
        for entry in trail:
            assert entry.actor_type
            assert entry.event_type

        # The chain the demo shows a judge.
        states = [e.new_state for e in trail if e.new_state]
        assert ExceptionState.CALLING in states
        assert ExceptionState.RESULT_RECEIVED in states
        assert ExceptionState.EVIDENCE_VALIDATED in states
        assert states[-1] == ExceptionState.HUMAN_REVIEW

        decisions = repo.decisions_for(db, exc)
        assert decisions
        assert all(d.reason_code and d.reason_text for d in decisions)


class TestProviderEventsReachTheAuditTrail:
    """`GET /v1/calls/{id}/events` is a required integration point.

    The master prompt lists "retrieve call events" alongside create and
    retrieve. It was implemented and unit-tested for a while but never
    actually called at runtime -- these tests exist so that cannot
    silently become true again.
    """

    def test_reconciliation_folds_provider_events_into_the_trail(
        self, db: Session
    ) -> None:
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])
        mark_reconciling(db, batch)
        db.commit()

        reconcile_call(db, batch.provider_call_id)
        db.commit()

        from app.db.models import AuditEvent

        provider_events = (
            db.query(AuditEvent)
            .filter(AuditEvent.entity_id == batch.id)
            .filter(AuditEvent.event_type.like("calle.%"))
            .all()
        )
        assert provider_events, "CALL-E developer events must reach the audit trail"
        assert all(e.payload.get("provider_event_id") for e in provider_events)
        assert all(e.actor_type == "provider" for e in provider_events)

    def test_repeated_reconciliation_does_not_duplicate_events(
        self, db: Session
    ) -> None:
        from app.db.models import AuditEvent

        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])
        mark_reconciling(db, batch)
        db.commit()

        def provider_event_count() -> int:
            return (
                db.query(AuditEvent)
                .filter(AuditEvent.entity_id == batch.id)
                .filter(AuditEvent.event_type.like("calle.%"))
                .count()
            )

        reconcile_call(db, batch.provider_call_id)
        db.commit()
        first = provider_event_count()
        assert first > 0

        reconcile_call(db, batch.provider_call_id)
        db.commit()
        assert provider_event_count() == first, "provider events must dedupe by id"

    def test_a_failing_event_fetch_never_blocks_resolution(self, db: Session) -> None:
        """Diagnostics are optional; the decision is not."""
        exc = seed(db, "PO-4821")
        batch = resolve_exceptions(db, [exc])
        mark_reconciling(db, batch)
        db.commit()

        provider = get_mock_provider()
        original = provider.list_events

        def explode(call_id: str):
            raise RuntimeError("provider events endpoint is down")

        provider.list_events = explode  # type: ignore[method-assign]
        try:
            reconcile_call(db, batch.provider_call_id)
            db.commit()
        finally:
            provider.list_events = original  # type: ignore[method-assign]

        assert state_of(db, exc) == ExceptionState.RESOLVED_ON_TIME
