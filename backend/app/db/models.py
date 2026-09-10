"""SQLAlchemy models.

Implements ``docs/data-model.md`` with the one structural change batch
calling requires: a :class:`CallBatch` sits between a workflow run and
the provider, because one CALL-E call task covers many exceptions
(``docs/provider-truth.md`` §3).

Concurrency controls from ``docs/architecture/low-level.md`` are declared
here as real constraints rather than left to application discipline:

* ``UNIQUE(workflow_run_id, attempt_no)`` -- one attempt row per logical
  attempt;
* ``UNIQUE(provider_event_id)`` -- duplicate webhook deliveries collide
  at the database rather than being deduplicated by a racy SELECT;
* ``UNIQUE(idempotency_key)`` on batches -- the same logical batch
  cannot be dispatched twice;
* ``UNIQUE(dedupe_key)`` on the outbox -- one side effect per decision;
* ``version`` columns -- optimistic concurrency (``spec.md`` §6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import (
    BatchStatus,
    CallAttemptStatus,
    ExceptionState,
    WorkflowRunState,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        server_default=func.now(),
        nullable=False,
    )


class ExceptionRecord(Base, TimestampMixin):
    """An operational exception: one purchase order needing a decision."""

    __tablename__ = "exceptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    po_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    recipient_phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    ack_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_ship_date: Mapped[date | None] = mapped_column(Date)
    item_summary: Mapped[str | None] = mapped_column(String(255))

    state: Mapped[str] = mapped_column(
        String(32), default=ExceptionState.OPEN, nullable=False, index=True
    )
    #: Optimistic concurrency. Every state change checks and bumps it.
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Operator-controlled. Reliability rule 15: no arbitrary targets.
    recipient_blocklisted: Mapped[bool] = mapped_column(Boolean, default=False)

    runs: Mapped[list[WorkflowRun]] = relationship(
        back_populates="exception", cascade="all, delete-orphan", order_by="WorkflowRun.created_at"
    )

    __table_args__ = (Index("ix_exceptions_state_due", "state", "ack_due_at"),)


class WorkflowRun(Base, TimestampMixin):
    """One resolution attempt-sequence for one exception."""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exception_id: Mapped[str] = mapped_column(
        ForeignKey("exceptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(
        String(32), default=WorkflowRunState.PLANNED, nullable=False
    )
    current_attempt_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exception: Mapped[ExceptionRecord] = relationship(back_populates="runs")
    attempts: Mapped[list[CallAttempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="CallAttempt.attempt_no"
    )


class CallBatch(Base, TimestampMixin):
    """One CALL-E call task, covering one or more attempts.

    This table is the structural addition the blueprint does not have.
    ``provider_call_id`` lives here rather than on the attempt, because
    a ``call_`` id identifies the whole batch.
    """

    __tablename__ = "call_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32), default="calle", nullable=False)
    #: UNIQUE: two batches must never share a provider call id. Without
    #: the constraint, a collision silently attaches one batch's attempts
    #: to a different batch's results.
    provider_call_id: Mapped[str | None] = mapped_column(
        String(128), index=True, unique=True
    )
    #: Content-derived from batch membership. UNIQUE, so a duplicate
    #: dispatch of the same logical batch fails at the database.
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=BatchStatus.PENDING, nullable=False, index=True
    )
    #: Whether this batch went to the real provider or the mock. Shown in
    #: the UI so a demo can never be mistaken for a live call.
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    task_text: Mapped[str | None] = mapped_column(Text)
    task_structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    task_completed: Mapped[bool | None] = mapped_column(Boolean)
    completion_confidence_score: Mapped[float | None] = mapped_column(Float)
    completion_confidence_label: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[list[str] | None] = mapped_column(JSONB)
    #: Stored verbatim for support. Never branched on -- no published
    #: enum (``docs/provider-truth.md`` §7).
    failure_code: Mapped[str | None] = mapped_column(String(128))
    failure_message: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Simulated-provider payload, stored only when ``is_live`` is false.
    #: The mock keeps its calls in memory, but the API and the worker are
    #: separate processes, so the worker could not otherwise reconcile a
    #: call the API created. Persisting the payload makes the webhook ->
    #: outbox -> worker -> reconcile path run identically in mock mode
    #: and live mode, which is the path we most want exercised.
    mock_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The simulated provider's developer-event stream, persisted for the
    #: same cross-process reason as ``mock_payload``. Without it the
    #: worker would reconcile a simulated call with no events, and the
    #: audit trail would silently lose CALL-E's own view of the call.
    mock_events: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    attempts: Mapped[list[CallAttempt]] = relationship(back_populates="batch")


class CallAttempt(Base, TimestampMixin):
    """One logical attempt to reach one supplier about one exception."""

    __tablename__ = "call_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_batches.id", ondelete="SET NULL"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Provider-assigned recipient id inside the call task. The join key
    #: that maps a batch webhook back onto this exception.
    provider_recipient_id: Mapped[str | None] = mapped_column(String(128), index=True)

    status: Mapped[str] = mapped_column(
        String(32), default=CallAttemptStatus.PENDING, nullable=False
    )
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    recipient_status: Mapped[str | None] = mapped_column(String(32))
    #: The conversation, as ``[{speaker, text, offset_seconds}, ...]``.
    #: Stored as evidence for a human reading the audit trail. It is
    #: never an input to policy -- the decision engine reads only the
    #: structured result, so no wording in a transcript can move a
    #: workflow.
    transcript: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(128))
    failure_message: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="attempts")
    batch: Mapped[CallBatch | None] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("workflow_run_id", "attempt_no", name="uq_attempt_per_run"),
    )


class WebhookEvent(Base):
    """Received provider events, deduplicated by provider event id."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    #: ``evt_*``. UNIQUE: an at-least-once redelivery collides here, so
    #: dedupe is a database guarantee rather than a racy check.
    provider_event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), index=True)
    event_type: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyDecisionRecord(Base):
    """Why the agent did what it did. One row per evaluated decision."""

    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exception_id: Mapped[str | None] = mapped_column(
        ForeignKey("exceptions.id", ondelete="CASCADE"), index=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    call_attempt_id: Mapped[str | None] = mapped_column(String(36))
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class AuditEvent(Base):
    """Append-only audit trail. Never updated, never deleted."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    new_state: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id", "created_at"),)


class OutboxMessage(Base):
    """Transactional outbox (``spec.md`` §7).

    A state change and its side effect commit together, so the
    "database changed but the call was never queued" split brain cannot
    happen. The worker drains this table; Redis only wakes it sooner.
    """

    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    #: One side effect per logical decision.
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
