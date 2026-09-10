"""Two dispatchers must never claim the same exception.

This is a regression test for a bug found by running the real stack: the
API and the worker both scanned for callable exceptions at the same
instant and planned *overlapping* batches. Because two batches with
different membership derive different idempotency keys, the provider had
no way to recognise them as the same work — so against live CALL-E this
would have placed two real phone calls to one supplier.

The fix is a row lock held from the eligibility check through the
CALL_PLANNED transition (``docs/architecture/low-level.md``: "database
row locks for stateful operations"). These tests race real PostgreSQL
sessions, because a lock that is only tested single-threaded is not
tested at all.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db import repositories as repo
from app.db.models import CallAttempt, CallBatch, ExceptionRecord
from app.domain.enums import ExceptionState
from app.services.call_orchestrator import resolve_exceptions
from app.services.provider import get_mock_provider
from tests.conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.integration]


def _now() -> datetime:
    return datetime.now(UTC)


def seed_many(db: Session, count: int) -> list[str]:
    ids = []
    for i in range(count):
        record = ExceptionRecord(
            po_number=f"PO-CC{i:03d}",
            supplier_name=f"Supplier {i}",
            recipient_name="Jordan",
            recipient_phone_e164=f"+1555010{i:04d}",
            ack_due_at=_now() - timedelta(hours=30),
            state=ExceptionState.OPEN,
        )
        db.add(record)
        # Flush before reading .id: it is a Python-side default, so it
        # is not assigned until the INSERT is emitted.
        db.flush()
        ids.append(record.id)
    db.commit()
    return ids


class TestConcurrentDispatchersDoNotOverlap:
    def test_two_dispatchers_never_claim_the_same_exception(self, engine) -> None:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        setup = factory()
        from sqlalchemy import text

        from app.db.models import Base

        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        setup.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        setup.commit()
        ids = seed_many(setup, 6)
        setup.close()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def dispatch() -> None:
            session = factory()
            try:
                claimed = repo.find_callable_exceptions(session, limit=6, claim=True)
                barrier.wait(timeout=10)  # maximise the overlap window
                if claimed:
                    resolve_exceptions(session, [c.id for c in claimed])
                session.commit()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=dispatch) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"dispatcher raised: {errors}"

        check = factory()
        try:
            attempts = check.query(CallAttempt).all()

            # The load-bearing assertion: one attempt per exception.
            # Two overlapping batches would produce two.
            runs = {a.workflow_run_id for a in attempts}
            assert len(attempts) == len(ids), (
                f"expected one attempt per exception, got {len(attempts)} "
                f"for {len(ids)} exceptions"
            )
            assert len(runs) == len(ids), "an exception was claimed by two batches"

            # And no exception is left behind.
            states = [e.state for e in check.query(ExceptionRecord).all()]
            assert all(s != ExceptionState.OPEN for s in states), states
        finally:
            check.close()

    def test_claiming_skips_rows_another_transaction_holds(self, engine) -> None:
        """`skip_locked` means the second reader gets a disjoint set."""
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        setup = factory()
        from sqlalchemy import text

        from app.db.models import Base

        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        setup.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        setup.commit()
        seed_many(setup, 4)
        setup.close()

        first = factory()
        second = factory()
        try:
            held = repo.find_callable_exceptions(first, limit=4, claim=True)
            assert len(held) == 4

            # While the first transaction holds all four, a second
            # dispatcher must see none of them -- not block, and above
            # all not return them.
            also = repo.find_callable_exceptions(second, limit=4, claim=True)
            assert also == [], "skip_locked must not hand out claimed rows"
        finally:
            first.rollback()
            second.rollback()
            first.close()
            second.close()

    def test_lock_exception_returns_none_when_already_claimed(self, engine) -> None:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        setup = factory()
        from sqlalchemy import text

        from app.db.models import Base

        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        setup.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        setup.commit()
        [exc_id] = seed_many(setup, 1)
        setup.close()

        holder = factory()
        contender = factory()
        try:
            assert repo.lock_exception(holder, exc_id) is not None
            assert repo.lock_exception(contender, exc_id) is None
        finally:
            holder.rollback()
            contender.rollback()
            holder.close()
            contender.close()


class TestNoStrandedAttempts:
    def test_an_uncorrelatable_attempt_is_released_for_retry(self, db: Session) -> None:
        """A terminal call with no slice for an attempt must not strand it.

        Before the fix the exception sat in CALLING forever, because the
        reconciliation sweep only looks at batches still marked
        DISPATCHED and this one had already gone terminal.
        """
        from app.adapters.calle.mapper import extract_webhook_call
        from app.services.result_processor import process_terminal_call

        [exc_id] = seed_many(db, 1)
        batch = resolve_exceptions(db, [exc_id])
        assert batch is not None

        # Break the correlation the way an overlapping batch did.
        attempt = repo.attempts_for_batch(db, batch.id)[0]
        attempt.provider_recipient_id = "call_does_not_exist_rcp99"
        db.commit()

        _, _, call = extract_webhook_call(
            get_mock_provider().webhook_body(batch.provider_call_id)
        )
        outcomes = process_terminal_call(db, call, reconciliation_available=False)
        db.commit()

        db.expire_all()
        state = db.get(ExceptionRecord, exc_id).state
        assert state != ExceptionState.CALLING, "exception must not be stranded"
        assert state == ExceptionState.RETRY_PENDING
        assert outcomes, "the release must be reported, not silently dropped"

        # And it must be explained, not silently retried.
        decisions = repo.decisions_for(db, exc_id)
        assert any("no result for this supplier" in d.reason_text for d in decisions)

    def test_no_business_outcome_is_invented_for_it(self, db: Session) -> None:
        from app.adapters.calle.mapper import extract_webhook_call
        from app.services.result_processor import process_terminal_call

        [exc_id] = seed_many(db, 1)
        batch = resolve_exceptions(db, [exc_id])
        attempt = repo.attempts_for_batch(db, batch.id)[0]
        attempt.provider_recipient_id = "call_does_not_exist_rcp99"
        db.commit()

        _, _, call = extract_webhook_call(
            get_mock_provider().webhook_body(batch.provider_call_id)
        )
        process_terminal_call(db, call, reconciliation_available=False)
        db.commit()

        db.expire_all()
        refreshed = repo.attempts_for_batch(db, batch.id)[0]
        assert refreshed.structured_result is None, "no evidence may be fabricated"
        assert db.get(ExceptionRecord, exc_id).state not in (
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
        )


def test_batch_rows_never_share_a_provider_call_id(db: Session) -> None:
    """Two batch rows pointing at one provider call means overlap happened."""
    ids = seed_many(db, 6)
    resolve_exceptions(db, ids)
    db.commit()

    call_ids = [b.provider_call_id for b in db.query(CallBatch).all()]
    assert len(call_ids) == len(set(call_ids)), f"duplicate provider call ids: {call_ids}"
