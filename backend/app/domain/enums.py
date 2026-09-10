"""Domain vocabulary for Resolve-E.

Every enum here is *internal*. Provider vocabulary lives in
``app.adapters.calle.schemas`` and is translated at the adapter boundary,
so that no provider string ever reaches the policy engine or the state
machine. See ``docs/provider-truth.md`` §7 for why this separation is
load-bearing rather than stylistic.
"""

from __future__ import annotations

from enum import StrEnum


class ExceptionState(StrEnum):
    """Lifecycle of an operational exception.

    Mirrors ``docs/hackathon-build/prd.md`` §5. The five states at the
    bottom are terminal: once an exception reaches one, no call result --
    however late -- may move it. That rule is enforced in
    ``app.domain.state_machine``, not by convention.
    """

    OPEN = "OPEN"
    ELIGIBLE = "ELIGIBLE"
    CALL_PLANNED = "CALL_PLANNED"
    CALLING = "CALLING"
    #: Webhook missing or uncertain; we are asking CALL-E for authoritative
    #: state. Present in ``docs/architecture/diagrams.md`` §2 but omitted
    #: from the ``prd.md`` §5 list. It is a real state, not a bookkeeping
    #: flag, because ``docs/demo.md`` surfaces it to the operator.
    RECONCILING = "RECONCILING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    EVIDENCE_VALIDATED = "EVIDENCE_VALIDATED"

    RESOLVED_ON_TIME = "RESOLVED_ON_TIME"
    RESOLVED_DELAYED = "RESOLVED_DELAYED"
    RETRY_PENDING = "RETRY_PENDING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLOSED_UNRESOLVED = "CLOSED_UNRESOLVED"


#: States from which no outbound transition is permitted.
#:
#: ``RETRY_PENDING`` is deliberately *not* terminal -- it is a holding
#: state that the scanner drains back into ``CALL_PLANNED``.
TERMINAL_EXCEPTION_STATES: frozenset[ExceptionState] = frozenset(
    {
        ExceptionState.RESOLVED_ON_TIME,
        ExceptionState.RESOLVED_DELAYED,
        ExceptionState.HUMAN_REVIEW,
        ExceptionState.CLOSED_UNRESOLVED,
    }
)


class WorkflowRunState(StrEnum):
    """Lifecycle of one resolution attempt-sequence for one exception."""

    PLANNED = "PLANNED"
    CALL_PLANNED = "CALL_PLANNED"
    CALLING = "CALLING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


TERMINAL_RUN_STATES: frozenset[WorkflowRunState] = frozenset(
    {WorkflowRunState.COMPLETED, WorkflowRunState.ABANDONED}
)


class CallAttemptStatus(StrEnum):
    """Status of one logical call attempt against one exception.

    Distinct from provider status: a provider call task is a *batch* and
    may cover many attempts. See ``docs/provider-truth.md`` §3.
    """

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class BatchStatus(StrEnum):
    """Status of one CALL-E call task covering one or more attempts."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    TERMINAL = "TERMINAL"
    FAILED = "FAILED"


class Outcome(StrEnum):
    """Internal outcome model from ``docs/hackathon-build/prd.md`` §4.

    This is what the *evidence policy* produces. It is not a state; the
    transition policy maps an outcome onto a state change.
    """

    CONFIRMED_ON_TIME = "confirmed_on_time"
    CONFIRMED_DELAYED = "confirmed_delayed"
    BLOCKED_NEED_HUMAN = "blocked_need_human"
    RECIPIENT_UNREACHABLE = "recipient_unreachable"
    ANSWER_UNCLEAR = "answer_unclear"
    CALL_FAILED = "call_failed"
    CANCELED_BY_POLICY = "canceled_by_policy"


class Decision(StrEnum):
    """What the policy engine instructs the orchestrator to do next."""

    RESOLVE_ON_TIME = "RESOLVE_ON_TIME"
    RESOLVE_DELAYED = "RESOLVE_DELAYED"
    RETRY = "RETRY"
    #: Structured result was absent but authoritative provider state has
    #: not been fetched yet. Distinct from RETRY: reconciling costs no
    #: phone call. ``docs/prompts/decision-engine.md`` requires it.
    RECONCILE = "RECONCILE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLOSE_UNRESOLVED = "CLOSE_UNRESOLVED"
    DO_NOT_CALL = "DO_NOT_CALL"
    PROCEED_WITH_CALL = "PROCEED_WITH_CALL"
    #: The exception is already terminal; the decision engine's first
    #: branch. Recorded so that late results leave an audit trail.
    NOOP = "NOOP"


class ErrorClass(StrEnum):
    """Error taxonomy from ``docs/architecture/low-level.md``.

    Only classes in :data:`RETRYABLE_ERROR_CLASSES` may drive an
    automatic retry. Everything else escalates or fails closed.
    """

    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    PERMANENT_PROVIDER = "PERMANENT_PROVIDER"
    CALL_OUTCOME = "CALL_OUTCOME"
    STRUCTURED_RESULT = "STRUCTURED_RESULT"
    POLICY = "POLICY"
    CONCURRENCY = "CONCURRENCY"
    INTERNAL = "INTERNAL"


RETRYABLE_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.RATE_LIMIT,
        ErrorClass.TRANSIENT_PROVIDER,
        ErrorClass.CALL_OUTCOME,
        ErrorClass.STRUCTURED_RESULT,
        ErrorClass.CONCURRENCY,
    }
)


class ActorType(StrEnum):
    """Who caused an audited event. Never ``llm`` -- see below."""

    SYSTEM = "system"
    OPERATOR = "operator"
    POLICY = "policy"
    PROVIDER = "provider"
    SCANNER = "scanner"


class ReasonCode(StrEnum):
    """Stable machine-readable reasons attached to every policy decision.

    These are surfaced in the UI and in the audit trail. They exist so a
    judge -- or an auditor -- can ask "why did the agent do that?" and
    get an answer that is not prose generated after the fact.
    """

    # eligibility
    NOT_YET_OVERDUE = "NOT_YET_OVERDUE"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NO_AUTHORIZED_PHONE = "NO_AUTHORIZED_PHONE"
    PHONE_NOT_E164 = "PHONE_NOT_E164"
    RECIPIENT_BLOCKLISTED = "RECIPIENT_BLOCKLISTED"
    QUIET_HOURS = "QUIET_HOURS"
    ELIGIBLE_FOR_CALL = "ELIGIBLE_FOR_CALL"

    # attempt budget
    ATTEMPT_BUDGET_AVAILABLE = "ATTEMPT_BUDGET_AVAILABLE"
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    RETRY_BACKOFF_NOT_ELAPSED = "RETRY_BACKOFF_NOT_ELAPSED"

    # disclosure / safety
    SENSITIVE_CONTENT_IN_TASK = "SENSITIVE_CONTENT_IN_TASK"
    TASK_WITHIN_DISCLOSURE_BUDGET = "TASK_WITHIN_DISCLOSURE_BUDGET"

    # evidence
    EVIDENCE_SUFFICIENT_ON_TIME = "EVIDENCE_SUFFICIENT_ON_TIME"
    EVIDENCE_SUFFICIENT_DELAYED = "EVIDENCE_SUFFICIENT_DELAYED"
    STRUCTURED_RESULT_MISSING = "STRUCTURED_RESULT_MISSING"
    STRUCTURED_RESULT_INVALID = "STRUCTURED_RESULT_INVALID"
    EVIDENCE_AMBIGUOUS = "EVIDENCE_AMBIGUOUS"
    SUPPLIER_REQUESTED_HUMAN = "SUPPLIER_REQUESTED_HUMAN"
    ORDER_BLOCKED = "ORDER_BLOCKED"
    NOT_RECEIVED = "NOT_RECEIVED"
    SHIP_DATE_UNUSABLE = "SHIP_DATE_UNUSABLE"
    LOW_COMPLETION_CONFIDENCE = "LOW_COMPLETION_CONFIDENCE"
    TASK_NOT_COMPLETED = "TASK_NOT_COMPLETED"
    RECIPIENT_NOT_REACHED = "RECIPIENT_NOT_REACHED"
    #: v2 evidence: identity and escalation are now recorded, not implied.
    RECIPIENT_NOT_IDENTIFIED = "RECIPIENT_NOT_IDENTIFIED"
    WRONG_PERSON = "WRONG_PERSON"
    SUPPLIER_DISPUTES_PO = "SUPPLIER_DISPUTES_PO"
    COMMERCIAL_CHANGE_REQUESTED = "COMMERCIAL_CHANGE_REQUESTED"
    ASKED_NOT_TO_BE_CALLED = "ASKED_NOT_TO_BE_CALLED"

    # provider
    PROVIDER_CALL_FAILED = "PROVIDER_CALL_FAILED"
    PROVIDER_RESULT_VALIDATION_FAILED = "PROVIDER_RESULT_VALIDATION_FAILED"

    # concurrency
    TERMINAL_STATE_PROTECTED = "TERMINAL_STATE_PROTECTED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
