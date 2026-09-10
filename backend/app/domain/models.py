"""Internal domain types.

These are the *only* types the policy engine sees. Provider objects are
normalised into them at the adapter boundary
(``app.adapters.calle.mapper``), which is what makes the policy engine
testable without a network and swappable without touching business
rules.

Everything here is frozen. Policy is a pure function of its inputs, and
a mutable input is an invitation to compute a decision from state that
has changed underneath it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.domain.enums import (
    CallAttemptStatus,
    Decision,
    ErrorClass,
    ExceptionState,
    ReasonCode,
)

#: The provider enforces this on ``recipients[].phones[]``. We validate
#: against the identical pattern before dispatch so an invalid number
#: fails inside our own validation layer, with an auditable reason code,
#: rather than as an opaque provider 400.
#: Source: ``calle.openapi.yaml`` -> ``CallTaskRecipientRequest.phones``.
E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


def is_valid_e164(phone: str | None) -> bool:
    return bool(phone) and bool(E164_PATTERN.match(phone or ""))


#: Dial code -> ISO 3166-1 alpha-2, longest prefix wins.
#:
#: Deliberately partial. ``region`` is documented as feeding routing and
#: compliance checks, so a *wrong* hint is worse than none: claiming a
#: US number when dialling +91 is a false statement about jurisdiction.
#: Unknown codes therefore send no hint and let CALL-E decide.
_DIAL_CODE_REGIONS: dict[str, tuple[str, str]] = {
    "1": ("US", "en-US"),
    "44": ("GB", "en-GB"),
    "61": ("AU", "en-AU"),
    "64": ("NZ", "en-NZ"),
    "65": ("SG", "en-SG"),
    "91": ("IN", "en-IN"),
    "353": ("IE", "en-IE"),
    "27": ("ZA", "en-ZA"),
    "971": ("AE", "en-AE"),
    "49": ("DE", "de-DE"),
    "33": ("FR", "fr-FR"),
    "34": ("ES", "es-ES"),
    "39": ("IT", "it-IT"),
    "31": ("NL", "nl-NL"),
    "81": ("JP", "ja-JP"),
    "55": ("BR", "pt-BR"),
    "52": ("MX", "es-MX"),
}


def region_and_locale_for(phone: str | None) -> tuple[str | None, str | None]:
    """Infer ``(region, locale)`` from an E.164 number.

    Returns ``(None, None)`` when the dial code is not recognised, so
    the request simply omits the hints rather than asserting something
    untrue about where the call is going.
    """
    if not is_valid_e164(phone):
        return (None, None)
    digits = (phone or "")[1:]
    # Longest prefix first: +1 must not shadow +1... vs +91.
    for length in (3, 2, 1):
        if hit := _DIAL_CODE_REGIONS.get(digits[:length]):
            return hit
    return (None, None)


@dataclass(frozen=True, slots=True)
class PolicySettings:
    """Deterministic knobs. Never inferred, never model-chosen.

    Defaults come from the example policy object in
    ``docs/prompts/decision-engine.md``.
    """

    max_attempts: int = 3
    delay_threshold_hours: int = 48
    #: Backoff before attempt N+1. Index 0 is the wait after attempt 1.
    #: ``reliability-and-failure.md`` §8: immediate, policy delay, final.
    retry_backoff_minutes: tuple[int, ...] = (0, 15, 60)
    #: Minimum task-level completion confidence required before a
    #: recipient may be promoted to a *resolved* state. Confidence can
    #: only ever withhold resolution -- never grant it.
    #: See ``docs/provider-truth.md`` §6.
    min_completion_confidence: float = 0.70
    #: Local-time window in which outbound calls are permitted.
    quiet_hours_start: int = 21
    quiet_hours_end: int = 8
    #: When false, quiet hours are not enforced -- used by the seeded
    #: demo so a judge can run it at any hour.
    enforce_quiet_hours: bool = False

    def backoff_minutes_for(self, attempt_no: int) -> int:
        """Minutes to wait before dispatching ``attempt_no``.

        Indexed so that ``retry_backoff_minutes[n - 1]`` is the wait
        before attempt ``n``: attempt 1 immediate, attempt 2 after the
        policy delay, attempt 3 the final bounded attempt
        (``reliability-and-failure.md`` §8).

        Beyond the configured schedule the last value repeats, so the
        policy is total rather than raising on an unexpected attempt
        number.
        """
        if attempt_no <= 1:
            return 0
        idx = min(attempt_no - 1, len(self.retry_backoff_minutes) - 1)
        return self.retry_backoff_minutes[idx]


@dataclass(frozen=True, slots=True)
class SupplierEvidence:
    """Validated structured result for one supplier.

    Field names are the *internal* ones. The wire renames ``status`` to
    ``po_status`` because CALL-E reserves ``status`` on recipient
    results -- see ``docs/provider-truth.md`` §3.
    """

    received: str
    status: str
    ship_date: str
    blocker: str
    needs_human: str
    #: v2. Who actually answered. A resolution requires this to be an
    #: authorized party -- an authorized *number* does not prove the
    #: person answering it is authorized.
    spoke_with: str = "unknown"
    #: v2. Why a person is needed, distinctly enough to act on.
    escalation_reason: str = "none"

    RECEIVED_VALUES = ("yes", "no", "unknown")
    SPOKE_WITH_VALUES = (
        "intended_contact",
        "authorized_representative",
        "wrong_person",
        "unknown",
    )
    ESCALATION_REASON_VALUES = (
        "none",
        "wants_human",
        "disputes_po",
        "commercial_change",
        "asked_not_to_be_called",
        "unknown",
    )
    #: Identities that may support an automatic resolution. Self-reported
    #: and therefore weak: this is authorization for a low-disclosure
    #: status question, not identity authentication. See
    #: ``docs/security-privacy.md``.
    AUTHORIZED_IDENTITIES = ("intended_contact", "authorized_representative")
    STATUS_VALUES = ("on_time", "delayed", "blocked", "unknown")
    BLOCKER_VALUES = (
        "none",
        "inventory",
        "production",
        "transport",
        "administrative",
        "unknown",
    )
    NEEDS_HUMAN_VALUES = ("yes", "no", "unknown")

    @property
    def is_ambiguous(self) -> bool:
        """True when the supplier did not clearly answer the core question."""
        return self.received == "unknown" or self.status == "unknown"

    @property
    def identity_supports_resolution(self) -> bool:
        """True when someone entitled to answer for the supplier did."""
        return self.spoke_with in self.AUTHORIZED_IDENTITIES

    @property
    def escalation_required(self) -> bool:
        """True when the recipient raised something a person must handle."""
        return self.escalation_reason not in ("none", "unknown")


@dataclass(frozen=True, slots=True)
class CompletionConfidence:
    """CALL-E's confidence in its own ``task_completed`` judgment."""

    score: float
    label: str


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """One spoken turn in a call.

    ``offset_seconds`` is nullable because the provider documents it as
    ``null`` when the source line carried no parseable timestamp.
    """

    #: ``bot`` | ``user`` | ``unknown``
    speaker: str
    text: str
    offset_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedAttempt:
    """One outbound dial attempt against one recipient.

    This is where the conversation lives -- ``transcript_turns`` hangs
    off ``recipients[].attempts[]``, not off the call task. It is
    evidence for a human reading the audit trail, and it is never an
    input to policy: the decision engine reads the structured result, so
    that no wording in a transcript can move a workflow.
    """

    provider_attempt_id: str
    #: ``queued`` | ``dialing`` | ``in_progress`` | ``completed`` | ``failed`` | ``canceled``
    status: str
    phone: str | None = None
    summary: str | None = None
    transcript: tuple[TranscriptTurn, ...] = ()
    #: Raw provider diagnostics. Displayed, never branched on.
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRecipient:
    """One recipient inside a provider call task, normalised."""

    provider_recipient_id: str
    phones: tuple[str, ...]
    #: ``pending`` | ``in_progress`` | ``completed`` | ``failed`` | ``skipped``
    status: str
    raw_structured_result: dict[str, Any] | None
    summary: str | None
    attempts: tuple[NormalizedAttempt, ...] = ()

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def transcript(self) -> tuple[TranscriptTurn, ...]:
        """The conversation, taken from the most recent attempt.

        Earlier attempts are dials that did not connect; the last one is
        the exchange that produced the evidence.
        """
        for attempt in reversed(self.attempts):
            if attempt.transcript:
                return attempt.transcript
        return ()

    @property
    def reached(self) -> bool:
        """True only when the provider says this recipient completed.

        ``skipped`` and ``failed`` both mean no usable conversation
        happened; neither may produce a business outcome.
        """
        return self.status == "completed"


@dataclass(frozen=True, slots=True)
class NormalizedCall:
    """A CALL-E call task, normalised into internal vocabulary.

    ``failure_code`` and ``failure_message`` are carried for display and
    support only. Nothing in the policy engine may branch on them --
    they have no published enum. See ``docs/provider-truth.md`` §7.
    """

    provider_call_id: str
    #: ``queued`` | ``in_progress`` | ``completed`` | ``failed`` | ``canceled``
    status: str
    recipients: tuple[NormalizedRecipient, ...]
    raw_structured_result: dict[str, Any] | None
    summary: str | None
    task_completed: bool | None
    completion_confidence: CompletionConfidence | None
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    TERMINAL_STATUSES = ("completed", "failed", "canceled")

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    def recipient_by_id(self, provider_recipient_id: str) -> NormalizedRecipient | None:
        for r in self.recipients:
            if r.provider_recipient_id == provider_recipient_id:
                return r
        return None


@dataclass(frozen=True, slots=True)
class ExceptionSnapshot:
    """Immutable view of an exception, as policy sees it.

    Deliberately not the ORM row: policy must not be able to lazily load
    a relationship, mutate state, or otherwise reach the database.
    """

    exception_id: str
    po_number: str
    supplier_name: str
    recipient_name: str | None
    recipient_phone_e164: str
    ack_due_at: datetime
    expected_ship_date: date | None
    state: ExceptionState
    version: int
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    last_attempt_status: CallAttemptStatus | None = None
    #: Operator-maintained. A blocklisted recipient is never dialled.
    recipient_blocklisted: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The output of every policy check.

    Always carries a machine-readable :class:`ReasonCode` alongside the
    human sentence, so the audit trail and the UI can explain *why* the
    agent acted without regenerating prose after the fact.
    """

    decision: Decision
    reason_code: ReasonCode
    reason_text: str
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    error_class: ErrorClass | None = None
    #: Set when the recipient asked not to be contacted again. The
    #: orchestrator blocklists them, so a later retry cannot dial back.
    suppress_future_calls: bool = False

    @property
    def allows_call(self) -> bool:
        return self.decision == Decision.PROCEED_WITH_CALL

    @property
    def is_resolution(self) -> bool:
        return self.decision in (Decision.RESOLVE_ON_TIME, Decision.RESOLVE_DELAYED)
