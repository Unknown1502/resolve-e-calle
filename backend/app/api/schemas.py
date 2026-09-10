"""Request and response models for the Resolve-E API.

These are the browser's entire view of the system. Note what is absent:
no API key and no raw provider payloads. The frontend gets exactly what
it needs to explain a decision -- which does include the transcript,
because "why did the agent conclude that?" is unanswerable without the
conversation it concluded from. Phone numbers are masked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import E164_PATTERN


class CreateExceptionRequest(BaseModel):
    po_number: str = Field(min_length=1, max_length=64)
    supplier_name: str = Field(min_length=1, max_length=255)
    recipient_name: str | None = Field(default=None, max_length=255)
    recipient_phone_e164: str
    ack_due_at: datetime
    expected_ship_date: date | None = None
    item_summary: str | None = Field(default=None, max_length=255)

    @field_validator("recipient_phone_e164")
    @classmethod
    def _e164(cls, v: str) -> str:
        # Reliability rule 15: no arbitrary phone targets from the UI.
        if not E164_PATTERN.match(v):
            raise ValueError("recipient_phone_e164 must be E.164, e.g. +15550001111")
        return v


class CreateExceptionResponse(BaseModel):
    exception_id: str
    state: str


class ResolveRequest(BaseModel):
    """Resolve one or more exceptions in a single call task."""

    exception_ids: list[str] = Field(default_factory=list, max_length=25)


class ResolveResponse(BaseModel):
    batch_id: str | None = None
    provider_call_id: str | None = None
    is_live: bool = False
    dispatched: int = 0
    refused: list[dict[str, str]] = Field(default_factory=list)
    message: str = ""


class AttemptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attempt_no: int
    status: str
    provider_recipient_id: str | None = None
    recipient_status: str | None = None
    structured_result: dict[str, Any] | None = None
    summary: str | None = None
    transcript: list[dict[str, Any]] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None


class BatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_call_id: str | None = None
    status: str
    is_live: bool
    idempotency_key: str
    summary: str | None = None
    task_completed: bool | None = None
    completion_confidence_score: float | None = None
    completion_confidence_label: str | None = None
    evidence: list[str] | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    task_structured_result: dict[str, Any] | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None


class DecisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    decision: str
    reason_code: str
    reason_text: str
    input_snapshot: dict[str, Any]
    created_at: datetime


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_type: str
    actor_type: str
    previous_state: str | None = None
    new_state: str | None = None
    payload: dict[str, Any]
    created_at: datetime


class ExceptionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    po_number: str
    supplier_name: str
    recipient_name: str | None = None
    state: str
    version: int
    ack_due_at: datetime
    expected_ship_date: date | None = None
    attempt_count: int = 0
    hours_overdue: float = 0.0
    last_reason_text: str | None = None
    is_live: bool = False


class ExceptionDetail(ExceptionSummary):
    recipient_phone_masked: str = ""
    item_summary: str | None = None
    attempts: list[AttemptView] = Field(default_factory=list)
    batches: list[BatchView] = Field(default_factory=list)
    decisions: list[DecisionView] = Field(default_factory=list)
    audit: list[AuditView] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str
    config: dict[str, Any]


def mask_phone(phone: str) -> str:
    """Show enough to recognise a number, not enough to redistribute it.

    ``docs/security-privacy.md`` asks logs and views to avoid carrying
    full phone numbers around unnecessarily.
    """
    if len(phone) <= 5:
        return "*" * len(phone)
    return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"
