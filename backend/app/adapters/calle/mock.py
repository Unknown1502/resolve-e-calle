"""Deterministic in-process CALL-E double.

``docs/hackathon-build/build-notes.md``:

    Never debug provider integration and business-state logic at the
    same time. Use a mock provider for domain tests and a real CALL-E
    smoke test for provider tests.

This double emits payloads in the *exact* shape of the OpenAPI
``CallTask`` schema, so ``mapper.map_call`` is exercised identically
whether the bytes came from here or from the network. That matters: a
mock that returns pre-normalised objects would hide mapping bugs, which
are precisely the bugs a provider integration has.

It also honours idempotency the way the provider documents it -- the
same key returns the original call rather than creating a second one --
so the duplicate-call guarantee is tested rather than asserted.

Scenarios are selected per recipient by PO number, which keeps the seeded
demo reproducible from an empty database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.adapters.calle.mapper import map_call
from app.domain.commands import CallHandle, CreateCallCommand
from app.domain.errors import ProviderTransientError, ValidationError
from app.domain.models import NormalizedCall, is_valid_e164


@dataclass(frozen=True, slots=True)
class RecipientScenario:
    """What the scripted supplier says, in wire shape."""

    #: ``RecipientStatus``: completed | failed | skipped
    status: str = "completed"
    result: dict[str, Any] | None = None
    summary: str | None = None
    transcript_line: str = ""


#: Scripted outcomes keyed by PO number. The three demo suppliers produce
#: three *different* policy decisions from one batch call, which is the
#: story ``docs/demo.md`` tells.
SCENARIOS: dict[str, RecipientScenario] = {
    # Clean confirmation, identity confirmed -> RESOLVE_ON_TIME
    "PO-4821": RecipientScenario(
        status="completed",
        result={
            "received": "yes",
            "po_status": "on_time",
            "ship_date": "2026-09-11",
            "blocker": "none",
            "needs_human": "no",
            "spoke_with": "intended_contact",
            "escalation_reason": "none",
        },
        summary="Supplier confirmed receipt of PO-4821 and an on-time ship date.",
        transcript_line="Yes, we have PO-4821. It ships Friday as planned.",
    ),
    # Delay with a usable date, identified via an authorized rep -> RESOLVE_DELAYED
    "PO-4822": RecipientScenario(
        status="completed",
        result={
            "received": "yes",
            "po_status": "delayed",
            # "tomorrow" rather than a weekday name so the demo is
            # time-of-day independent: ack_due is 20h back, so its DATE
            # is today or yesterday, making the slip 24h or 48h -- inside
            # the 48h threshold either way, at any hour the judge runs it.
            "ship_date": "tomorrow",
            "blocker": "inventory",
            "needs_human": "no",
            "spoke_with": "authorized_representative",
            "escalation_reason": "none",
        },
        summary="Supplier confirmed receipt but reported an inventory delay.",
        transcript_line="We received it, but inventory is delayed. We can ship next Tuesday.",
    ),
    # Hedged, second-hand answer, nobody's identity established -> HUMAN_REVIEW
    "PO-4823": RecipientScenario(
        status="completed",
        result={
            "received": "unknown",
            "po_status": "unknown",
            "ship_date": "unknown",
            "blocker": "unknown",
            "needs_human": "no",
            "spoke_with": "unknown",
            "escalation_reason": "unknown",
        },
        summary="Answer was second-hand and unclear.",
        transcript_line="I believe someone in logistics has it.",
    ),
    # Commercial ask, distinctly coded -> HUMAN_REVIEW via COMMERCIAL_CHANGE_REQUESTED
    # (previously indistinguishable from any other needs_human=yes case)
    "PO-4824": RecipientScenario(
        status="completed",
        result={
            "received": "yes",
            "po_status": "delayed",
            "ship_date": "tomorrow",
            "blocker": "none",
            "needs_human": "yes",
            "spoke_with": "intended_contact",
            "escalation_reason": "commercial_change",
        },
        summary="Supplier raised pricing; requires a human decision.",
        transcript_line="We can ship tomorrow if you increase the price.",
    ),
    # Never reached -> RETRY, no business outcome
    "PO-4825": RecipientScenario(
        status="failed",
        result=None,
        summary=None,
        transcript_line="",
    ),
    # Schema-invalid extraction -> STRUCTURED_RESULT, reconcile then escalate
    "PO-4826": RecipientScenario(
        status="completed",
        result=None,
        summary="Call completed but no schema-valid result could be extracted.",
        transcript_line="... [inaudible] ...",
    ),
    # Right number, wrong person -> HUMAN_REVIEW via WRONG_PERSON.
    # Demonstrates that an authorized DESTINATION does not authorize
    # whoever happens to answer it: a clear "yes, on time" from the
    # wrong person still cannot close the exception.
    "PO-4827": RecipientScenario(
        status="completed",
        result={
            "received": "yes",
            "po_status": "on_time",
            "ship_date": "2026-09-12",
            "blocker": "none",
            "needs_human": "no",
            "spoke_with": "wrong_person",
            "escalation_reason": "none",
        },
        summary="Reached the desk, but not the purchasing contact for this order.",
        transcript_line="I'm not the right person for POs, you'd want procurement.",
    ),
}

DEFAULT_SCENARIO = SCENARIOS["PO-4821"]


def _scripted_transcript(rc, scenario: RecipientScenario) -> list[dict[str, Any]]:
    """A short, plausible exchange for the scripted supplier.

    The bot's lines follow the question order in the task template, so
    the transcript a viewer reads matches the instructions the agent was
    actually given.
    """
    if not scenario.transcript_line:
        return []
    return [
        {
            "offset_seconds": 0,
            "speaker": "bot",
            "text": (
                f"Hello, this is Resolve-E calling on behalf of the purchasing team "
                f"about purchase order {rc.po_number}. Am I speaking with the right "
                f"contact at {rc.supplier_name}?"
            ),
        },
        {"offset_seconds": 6, "speaker": "user", "text": "Yes, speaking."},
        {
            "offset_seconds": 8,
            "speaker": "bot",
            "text": f"Thank you. Can you confirm whether you received {rc.po_number}?",
        },
        {"offset_seconds": 13, "speaker": "user", "text": scenario.transcript_line},
        {
            "offset_seconds": 19,
            "speaker": "bot",
            "text": "Understood. Thank you for your time.",
        },
    ]


@dataclass
class MockCalleProvider:
    """In-memory CALL-E. Satisfies :class:`~app.adapters.calle.client.CallProvider`."""

    #: Idempotency key -> call id, mirroring provider dedupe semantics.
    _by_key: dict[str, str] = field(default_factory=dict)
    _calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    _events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    #: Raise a transient error on the next create, once. Used to prove
    #: that a timed-out create retried with the same key does not place
    #: a second call.
    fail_next_create: bool = False
    #: Hold calls in ``in_progress`` so reconciliation can be exercised.
    defer_terminal: bool = False

    #: Every create ever received, for duplicate-call assertions.
    create_log: list[str] = field(default_factory=list)

    # ── CallProvider ─────────────────────────────────────────────────

    def create_call(self, command: CreateCallCommand) -> CallHandle:
        for r in command.recipients:
            if not is_valid_e164(r.phone_e164):
                raise ValidationError(f"recipient phone {r.phone_e164!r} is not valid E.164")

        if self.fail_next_create:
            self.fail_next_create = False
            # The provider may well have accepted it. This is exactly the
            # case that makes a fresh idempotency key dangerous.
            raise ProviderTransientError("simulated timeout after provider receipt")

        self.create_log.append(command.idempotency_key)

        if existing := self._by_key.get(command.idempotency_key):
            raw = self._calls[existing]
            return CallHandle(
                provider_call_id=existing,
                status=raw["status"],
                provider_recipient_ids=tuple(r["id"] for r in raw["recipients"]),
                deduplicated=True,
            )

        # Globally unique, NOT a per-process counter. The api and the
        # worker are separate processes with separate mock instances; a
        # counter starting at 1 in each mints "call_mock0001" twice, and
        # two unrelated batches then collide on one provider call id.
        # Real call ids are globally unique, so the double must be too.
        call_id = f"call_mock{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        terminal = not self.defer_terminal

        recipients: list[dict[str, Any]] = []
        for idx, rc in enumerate(command.recipients, start=1):
            scenario = SCENARIOS.get(rc.po_number, DEFAULT_SCENARIO)
            recipients.append(
                {
                    "id": f"{call_id}_rcp{idx}",
                    # Not a provider field. map_recipient ignores unknown
                    # keys, and deferred finalisation needs to re-resolve
                    # which scripted supplier this recipient was.
                    "_po_number": rc.po_number,
                    "phones": [rc.phone_e164],
                    "locale": rc.locale,
                    "region": rc.region,
                    "status": scenario.status if terminal else "in_progress",
                    "structured_result": scenario.result if terminal else None,
                    "summary": scenario.summary if terminal else None,
                    "attempts": [
                        {
                            "id": f"{call_id}_att{idx}",
                            "phone": rc.phone_e164,
                            "status": "completed"
                            if scenario.status == "completed"
                            else "failed",
                            "started_at": now.isoformat(),
                            "completed_at": now.isoformat() if terminal else None,
                            "summary": scenario.summary if terminal else None,
                            "transcript_turns": _scripted_transcript(rc, scenario)
                            if terminal
                            else [],
                            "provider_call_id": None,
                            "failure_code": None
                            if scenario.status == "completed"
                            else "no_answer",
                            "failure_message": None
                            if scenario.status == "completed"
                            else "Recipient did not answer.",
                        }
                    ],
                }
            )

        reached = sum(1 for r in recipients if r["status"] == "completed")
        confirmed = sum(
            1
            for r in recipients
            if isinstance(r["structured_result"], dict)
            and r["structured_result"].get("received") == "yes"
        )

        raw_call: dict[str, Any] = {
            "id": call_id,
            "object": "call_task",
            "status": "completed" if terminal else "in_progress",
            "task": command.task,
            "recipients": recipients,
            "structured_result": (
                {"suppliers_reached": reached, "suppliers_confirmed": confirmed}
                if terminal
                else None
            ),
            "summary": f"Called {len(recipients)} supplier(s)." if terminal else None,
            "task_completed": True if terminal else None,
            "completion_confidence": (
                {"score": 0.91, "label": "high"} if terminal else None
            ),
            "evidence": (
                [s.transcript_line for s in self._scenarios_for(command) if s.transcript_line]
                if terminal
                else []
            ),
            "metadata": command.metadata,
            "failure_code": None,
            "failure_message": None,
            "created_at": now.isoformat(),
            "completed_at": now.isoformat() if terminal else None,
        }

        self._calls[call_id] = raw_call
        self._by_key[command.idempotency_key] = call_id
        self._events[call_id] = [
            {
                "id": f"evt_{call_id}_created",
                "type": "call.created",
                "call_id": call_id,
                "created_at": now.isoformat(),
                "level": "info",
                "status": raw_call["status"],
                "message": "Call task accepted.",
                "details": {"recipient_count": len(recipients)},
            }
        ]

        return CallHandle(
            provider_call_id=call_id,
            status=raw_call["status"],
            provider_recipient_ids=tuple(r["id"] for r in recipients),
            deduplicated=False,
        )

    def get_call(self, call_id: str) -> NormalizedCall:
        return map_call(self.raw_call(call_id))

    def knows(self, call_id: str) -> bool:
        """True when this process's mock holds the call in memory."""
        return call_id in self._calls

    def raw_call(self, call_id: str) -> dict[str, Any]:
        """The provider-shaped payload, for persisting across processes."""
        raw = self._calls.get(call_id)
        if raw is None:
            raise ValidationError(f"unknown call id {call_id!r}")
        return raw

    def list_events(self, call_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(call_id, []))

    # ── test / demo helpers ──────────────────────────────────────────

    def _scenarios_for(self, command: CreateCallCommand) -> list[RecipientScenario]:
        return [SCENARIOS.get(r.po_number, DEFAULT_SCENARIO) for r in command.recipients]

    def webhook_body(
        self, call_id: str, *, event_type: str = "call.completed", event_id: str | None = None
    ) -> dict[str, Any]:
        """Build the terminal webhook CALL-E would POST for this call."""
        raw = self._calls.get(call_id)
        if raw is None:
            raise ValidationError(f"unknown call id {call_id!r}")
        return {
            "id": event_id or f"evt_{call_id}_terminal",
            "type": event_type,
            "created_at": datetime.now(UTC).isoformat(),
            "data": raw,
        }

    def finalize(self, call_id: str) -> None:
        """Move a deferred call to terminal, as reconciliation would find it."""
        raw = self._calls.get(call_id)
        if raw is None:
            raise ValidationError(f"unknown call id {call_id!r}")
        now = datetime.now(UTC)
        raw["status"] = "completed"
        raw["task_completed"] = True
        raw["completion_confidence"] = {"score": 0.91, "label": "high"}
        raw["completed_at"] = now.isoformat()
        reached = 0
        confirmed = 0
        for r in raw["recipients"]:
            scenario = SCENARIOS.get(r.get("_po_number", ""), DEFAULT_SCENARIO)
            if r["status"] == "in_progress":
                r["status"] = scenario.status
                r["structured_result"] = scenario.result
                r["summary"] = scenario.summary
            if r["status"] == "completed":
                reached += 1
                if (r["structured_result"] or {}).get("received") == "yes":
                    confirmed += 1
        raw["structured_result"] = {
            "suppliers_reached": reached,
            "suppliers_confirmed": confirmed,
        }

    def fail_call(self, call_id: str, *, failure_code: str = "no_answer") -> None:
        """Mark a call as terminally failed, for provider-failure tests."""
        raw = self._calls[call_id]
        raw["status"] = "failed"
        raw["failure_code"] = failure_code
        raw["failure_message"] = "Simulated provider failure."
        raw["task_completed"] = False
        raw["completed_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        for r in raw["recipients"]:
            r["status"] = "failed"
            r["structured_result"] = None

    @property
    def call_count(self) -> int:
        """Distinct calls actually created -- the duplicate-call metric."""
        return len(self._calls)
