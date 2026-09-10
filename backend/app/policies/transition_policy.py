"""The deterministic decision engine.

This is the executable form of the pseudocode in
``docs/prompts/decision-engine.md``. The branch order there is
normative and is preserved exactly, because the order encodes
precedence: ``needs_human`` outranks a clean ``on_time`` answer, and a
terminal exception outranks everything.

**No language model participates in this function.** It takes validated
evidence and deterministic context and returns a decision. That is the
entire product thesis -- ``docs/00-product-vision.md``: "LLM output is
evidence, not authority."

The function is pure and total: every path returns a
:class:`PolicyDecision` carrying a :class:`ReasonCode`, so no outcome
can reach the database without an auditable justification.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import Decision, ExceptionState, ReasonCode
from app.domain.models import (
    ExceptionSnapshot,
    NormalizedCall,
    NormalizedRecipient,
    PolicyDecision,
    PolicySettings,
)
from app.domain.state_machine import is_terminal
from app.policies.attempt_policy import check_attempt_budget
from app.policies.evidence_policy import (
    confidence_permits_resolution,
    validate_recipient_result,
)
from app.policies.ship_date import delay_within_policy, parse_ship_date


def _budget_exhausted(budget: PolicyDecision) -> bool:
    """True only when no further attempt will ever be permitted.

    A budget answer of "not yet" -- backoff still running, or an attempt
    already in flight -- is **not** exhaustion. Those cases stay in
    RETRY_PENDING and are picked up by the scanner once the wait
    elapses. Escalating them to a human would turn a scheduling delay
    into a false alarm on someone's queue.
    """
    return budget.reason_code is ReasonCode.ATTEMPT_BUDGET_EXHAUSTED


def decide(
    exception: ExceptionSnapshot,
    call: NormalizedCall,
    recipient: NormalizedRecipient,
    settings: PolicySettings,
    *,
    now: datetime | None = None,
    reconciliation_available: bool = True,
) -> PolicyDecision:
    """Decide what to do with one supplier's result from one call task.

    Args:
        exception: The purchase-order exception being resolved.
        call: The whole normalised CALL-E call task (batch-level fields).
        recipient: This supplier's slice of that call task.
        settings: Deterministic policy knobs.
        now: Injected clock, for reproducible tests.
        reconciliation_available: False once we have already fetched
            authoritative provider state and *still* have no result --
            at that point reconciling again cannot help.

    Returns:
        A decision with a machine-readable reason code.
    """
    now = now or datetime.now(UTC)
    snapshot: dict[str, Any] = {
        "exception_id": exception.exception_id,
        "po_number": exception.po_number,
        "exception_state": str(exception.state),
        "provider_call_id": call.provider_call_id,
        "provider_recipient_id": recipient.provider_recipient_id,
        "call_status": call.status,
        "recipient_status": recipient.status,
        "attempt_count": exception.attempt_count,
        "evaluated_at": now.isoformat(),
    }

    def decision(
        d: Decision, code: ReasonCode, text: str, **extra: Any
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=d,
            reason_code=code,
            reason_text=text,
            input_snapshot={**snapshot, **extra},
        )

    # ── 1. Terminal exceptions absorb late results ────────────────────
    # reliability-and-failure.md §7: store the result as historical
    # evidence, never overwrite the state.
    if is_terminal(exception.state):
        return decision(
            Decision.NOOP,
            ReasonCode.TERMINAL_STATE_PROTECTED,
            f"Exception is already {exception.state}; recording this result as "
            "historical evidence without changing state.",
        )

    # ── 2. Provider-level call failure ────────────────────────────────
    # Branches on CallStatus, a published enum -- never on failure_code.
    if call.status in ("failed", "canceled"):
        budget = check_attempt_budget(exception, settings, now=now)
        if _budget_exhausted(budget):
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.PROVIDER_CALL_FAILED,
                f"CALL-E call task ended as '{call.status}' and {budget.reason_text.lower()}",
                provider_failure_code=call.failure_code,
            )
        return decision(
            Decision.RETRY,
            ReasonCode.PROVIDER_CALL_FAILED,
            f"CALL-E call task ended as '{call.status}'; retrying within the "
            f"attempt budget ({exception.attempt_count}/{settings.max_attempts} used).",
            provider_failure_code=call.failure_code,
        )

    # ── 3. Evidence re-validation ─────────────────────────────────────
    validation = validate_recipient_result(recipient)

    if not validation.ok:
        # Structurally unusable. Reconcile first -- it costs no phone
        # call -- then retry, then escalate.
        if reconciliation_available:
            return decision(
                Decision.RECONCILE,
                validation.reason_code,
                f"{validation.reason_text} Fetching authoritative call state from "
                "CALL-E before deciding.",
            )

        budget = check_attempt_budget(exception, settings, now=now)
        if _budget_exhausted(budget):
            return decision(
                Decision.HUMAN_REVIEW,
                validation.reason_code,
                f"{validation.reason_text} No retry budget remains; escalating to a human.",
            )
        return decision(
            Decision.RETRY,
            validation.reason_code,
            f"{validation.reason_text} Authoritative state confirmed; retrying.",
        )

    evidence = validation.evidence
    assert evidence is not None  # guarded by validation.ok
    snapshot["evidence"] = {
        "received": evidence.received,
        "status": evidence.status,
        "ship_date": evidence.ship_date,
        "blocker": evidence.blocker,
        "needs_human": evidence.needs_human,
    }

    snapshot["evidence"]["spoke_with"] = evidence.spoke_with
    snapshot["evidence"]["escalation_reason"] = evidence.escalation_reason

    # ── 4. Asked not to be called again ───────────────────────────────
    # Ordered first among escalations because it carries a *consequence*
    # beyond this workflow: the contact is suppressed, so no later retry
    # can dial them back. Honouring that is not optional.
    if evidence.escalation_reason == "asked_not_to_be_called":
        return PolicyDecision(
            decision=Decision.HUMAN_REVIEW,
            reason_code=ReasonCode.ASKED_NOT_TO_BE_CALLED,
            reason_text=(
                "Recipient asked not to be contacted again. Suppressing further "
                "calls to this contact and escalating to a person."
            ),
            input_snapshot=snapshot,
            suppress_future_calls=True,
        )

    # ── 5. Other escalation triggers, distinctly ──────────────────────
    # These used to collapse into needs_human, which told an operator
    # that a person was needed but never why.
    _ESCALATIONS = {
        "disputes_po": (
            ReasonCode.SUPPLIER_DISPUTES_PO,
            "Supplier disputes this purchase order; a person must reconcile the record.",
        ),
        "commercial_change": (
            ReasonCode.COMMERCIAL_CHANGE_REQUESTED,
            "Supplier raised pricing, payment terms or a contract change. The agent "
            "has no authority to agree to any of it.",
        ),
        "wants_human": (
            ReasonCode.SUPPLIER_REQUESTED_HUMAN,
            "Supplier asked to speak to a person.",
        ),
    }
    if hit := _ESCALATIONS.get(evidence.escalation_reason):
        esc_code, esc_text = hit
        return decision(Decision.HUMAN_REVIEW, esc_code, esc_text)

    # ── 6. Supplier asked for a human ─────────────────────────────────
    # Outranks every positive answer, per the pseudocode's ordering.
    if evidence.needs_human == "yes":
        return decision(
            Decision.HUMAN_REVIEW,
            ReasonCode.SUPPLIER_REQUESTED_HUMAN,
            "Supplier requested human follow-up or a consequential decision is required.",
        )

    # ── 7. Identity gate ──────────────────────────────────────────────
    # An authorized *number* does not prove the person who answered it is
    # authorized to speak for the supplier. Only gates outcomes that would
    # close the exception; a retry or an escalation needs no identity.
    could_close = evidence.received == "yes" and evidence.status in ("on_time", "delayed")
    if could_close and not evidence.identity_supports_resolution:
        if evidence.spoke_with == "wrong_person":
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.WRONG_PERSON,
                "The person reached said they are not the right contact for this "
                "purchase order, so their answers cannot close it.",
            )
        return decision(
            Decision.HUMAN_REVIEW,
            ReasonCode.RECIPIENT_NOT_IDENTIFIED,
            "Nobody confirmed they were the supplier contact or an authorized "
            "representative, so this answer cannot close the purchase order.",
        )

    # ── 8. Confidence dampener ────────────────────────────────────────
    # Applied only to paths that could *resolve*. It withholds; it never
    # grants. docs/provider-truth.md §6.
    could_resolve = evidence.received == "yes" and evidence.status in ("on_time", "delayed")
    if could_resolve:
        permitted, code, text = confidence_permits_resolution(call, settings)
        if not permitted:
            assert code is not None
            return decision(
                Decision.HUMAN_REVIEW,
                code,
                f"{text} The supplier's answer was clear, but Resolve-E will not close "
                "a purchase order on low-confidence call evidence.",
            )
        snapshot["completion_confidence"] = text

    # ── 9. Clean confirmation, on time ────────────────────────────────
    if evidence.received == "yes" and evidence.status == "on_time":
        return decision(
            Decision.RESOLVE_ON_TIME,
            ReasonCode.EVIDENCE_SUFFICIENT_ON_TIME,
            f"Supplier confirmed receipt of {exception.po_number} and an on-time schedule.",
        )

    # ── 10. Delay, gated on a usable date within threshold ─────────────
    if evidence.status == "delayed":
        parsed = parse_ship_date(evidence.ship_date, reference=now.date())
        if parsed is None:
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.SHIP_DATE_UNUSABLE,
                f"Supplier reported a delay but the stated ship date "
                f"({evidence.ship_date!r}) could not be resolved to a specific date.",
            )
        if not delay_within_policy(parsed, exception.ack_due_at, settings.delay_threshold_hours):
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.EVIDENCE_AMBIGUOUS,
                f"Stated ship date {parsed.isoformat()} exceeds the "
                f"{settings.delay_threshold_hours}h delay threshold; a human must accept "
                "or reject this slip.",
                parsed_ship_date=parsed.isoformat(),
            )
        if evidence.received != "yes":
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.NOT_RECEIVED,
                "Supplier described a delay without confirming they received the "
                "purchase order; the two statements are inconsistent.",
            )
        return decision(
            Decision.RESOLVE_DELAYED,
            ReasonCode.EVIDENCE_SUFFICIENT_DELAYED,
            f"Supplier confirmed receipt and a delay to {parsed.isoformat()}, which is "
            f"within the {settings.delay_threshold_hours}h threshold.",
            parsed_ship_date=parsed.isoformat(),
            blocker=evidence.blocker,
        )

    # ── 11. Explicitly blocked ─────────────────────────────────────────
    if evidence.status == "blocked":
        return decision(
            Decision.HUMAN_REVIEW,
            ReasonCode.ORDER_BLOCKED,
            f"Supplier reported the order is blocked (cause: {evidence.blocker}).",
        )

    # ── 12. Not received -- retry or escalate ──────────────────────────
    if evidence.received == "no":
        budget = check_attempt_budget(exception, settings, now=now)
        if _budget_exhausted(budget):
            return decision(
                Decision.HUMAN_REVIEW,
                ReasonCode.NOT_RECEIVED,
                "Supplier states the purchase order was never received and the attempt "
                "budget is exhausted.",
            )
        return decision(
            Decision.RETRY,
            ReasonCode.NOT_RECEIVED,
            "Supplier states the purchase order was never received; retrying to "
            "reach the correct contact.",
        )

    # ── 13. Default: ambiguity is never resolved ──────────────────────
    return decision(
        Decision.HUMAN_REVIEW,
        ReasonCode.EVIDENCE_AMBIGUOUS,
        f"Evidence is insufficient to decide (received={evidence.received}, "
        f"status={evidence.status}); escalating rather than guessing.",
    )


#: Which exception state a decision moves the workflow into.
#:
#: Kept as data next to the engine so that "what did we decide" and
#: "where does that put the workflow" cannot drift apart.
DECISION_TO_STATE: dict[Decision, ExceptionState | None] = {
    Decision.RESOLVE_ON_TIME: ExceptionState.RESOLVED_ON_TIME,
    Decision.RESOLVE_DELAYED: ExceptionState.RESOLVED_DELAYED,
    Decision.RETRY: ExceptionState.RETRY_PENDING,
    Decision.RECONCILE: ExceptionState.RECONCILING,
    Decision.HUMAN_REVIEW: ExceptionState.HUMAN_REVIEW,
    Decision.CLOSE_UNRESOLVED: ExceptionState.CLOSED_UNRESOLVED,
    Decision.DO_NOT_CALL: ExceptionState.HUMAN_REVIEW,
    Decision.NOOP: None,
    Decision.PROCEED_WITH_CALL: None,
}
