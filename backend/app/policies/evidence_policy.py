"""Re-validation of CALL-E structured results.

CALL-E validates the result against the schema we submitted. We validate
it *again*, here, and the reason is stated by the provider's own
documentation:

    Descriptions guide extraction but are not hard validation rules.
    Hard validation comes from `type`, `required`, `enum`, and
    `additionalProperties`.

So the enum membership is guaranteed; the *semantics* are not. Nothing
stops a schema-valid result from saying ``received=yes`` while the
recipient was never reached. This module is the boundary where a
provider result stops being a claim and becomes evidence -- or is
refused.

Three rules, none of which the provider can enforce for us:

1. A recipient who was not *reached* cannot produce a business outcome,
   whatever their structured result says.
2. Missing, null, or non-conforming results are ``STRUCTURED_RESULT``
   errors -- never silently coerced to ``unknown``.
3. Task-level completion confidence may only ever *withhold* a
   resolution, never grant one. See ``docs/provider-truth.md`` §6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import Outcome, ReasonCode
from app.domain.models import (
    NormalizedCall,
    NormalizedRecipient,
    PolicySettings,
    SupplierEvidence,
)

#: Wire name -> internal name. CALL-E reserves ``status`` on recipient
#: results, so the schema we submit calls it ``po_status``.
#: See ``docs/provider-truth.md`` §3.
WIRE_TO_INTERNAL: dict[str, str] = {
    "received": "received",
    "po_status": "status",
    "ship_date": "ship_date",
    "blocker": "blocker",
    "needs_human": "needs_human",
    "spoke_with": "spoke_with",
    "escalation_reason": "escalation_reason",
}

_ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "received": SupplierEvidence.RECEIVED_VALUES,
    "status": SupplierEvidence.STATUS_VALUES,
    "blocker": SupplierEvidence.BLOCKER_VALUES,
    "needs_human": SupplierEvidence.NEEDS_HUMAN_VALUES,
    "spoke_with": SupplierEvidence.SPOKE_WITH_VALUES,
    "escalation_reason": SupplierEvidence.ESCALATION_REASON_VALUES,
}


@dataclass(frozen=True, slots=True)
class EvidenceValidation:
    """Outcome of re-validating one recipient's structured result."""

    evidence: SupplierEvidence | None
    reason_code: ReasonCode
    reason_text: str
    #: Set when the result was structurally unusable, as opposed to
    #: usable-but-ambiguous. Drives the RECONCILE / RETRY branch.
    is_structural_failure: bool = False

    @property
    def ok(self) -> bool:
        return self.evidence is not None


def validate_recipient_result(
    recipient: NormalizedRecipient,
) -> EvidenceValidation:
    """Turn a raw recipient result into :class:`SupplierEvidence`, or refuse."""

    # Rule 1: no conversation, no evidence. A "skipped" or "failed"
    # recipient may still carry a structured result object; it describes
    # nothing that happened and must not reach the decision engine.
    if not recipient.reached:
        return EvidenceValidation(
            evidence=None,
            reason_code=ReasonCode.RECIPIENT_NOT_REACHED,
            reason_text=(
                f"Recipient status is '{recipient.status}'; no conversation took place, "
                "so no business evidence exists."
            ),
            is_structural_failure=True,
        )

    raw = recipient.raw_structured_result

    # Rule 2: null is never success. reliability-and-failure.md §5.
    if raw is None:
        return EvidenceValidation(
            evidence=None,
            reason_code=ReasonCode.STRUCTURED_RESULT_MISSING,
            reason_text=(
                "CALL-E returned no schema-valid structured result for this recipient."
            ),
            is_structural_failure=True,
        )

    if not isinstance(raw, dict):
        return EvidenceValidation(
            evidence=None,
            reason_code=ReasonCode.STRUCTURED_RESULT_INVALID,
            reason_text=f"Structured result is {type(raw).__name__}, expected an object.",
            is_structural_failure=True,
        )

    missing = [w for w in WIRE_TO_INTERNAL if w not in raw]
    if missing:
        return EvidenceValidation(
            evidence=None,
            reason_code=ReasonCode.STRUCTURED_RESULT_INVALID,
            reason_text=f"Structured result is missing required fields: {', '.join(missing)}.",
            is_structural_failure=True,
        )

    # additionalProperties: false is declared on the wire, but we do not
    # rely on the provider to have enforced it.
    if extra := [k for k in raw if k not in WIRE_TO_INTERNAL]:
        return EvidenceValidation(
            evidence=None,
            reason_code=ReasonCode.STRUCTURED_RESULT_INVALID,
            reason_text=f"Structured result carries undeclared fields: {', '.join(sorted(extra))}.",
            is_structural_failure=True,
        )

    values: dict[str, Any] = {
        internal: raw[wire] for wire, internal in WIRE_TO_INTERNAL.items()
    }

    for name, value in values.items():
        if not isinstance(value, str):
            return EvidenceValidation(
                evidence=None,
                reason_code=ReasonCode.STRUCTURED_RESULT_INVALID,
                reason_text=f"Field '{name}' is {type(value).__name__}, expected a string.",
                is_structural_failure=True,
            )
        allowed = _ALLOWED_VALUES.get(name)
        if allowed is not None and value not in allowed:
            return EvidenceValidation(
                evidence=None,
                reason_code=ReasonCode.STRUCTURED_RESULT_INVALID,
                reason_text=(
                    f"Field '{name}' has value '{value}', which is outside the "
                    f"declared enum {list(allowed)}."
                ),
                is_structural_failure=True,
            )

    evidence = SupplierEvidence(**values)
    return EvidenceValidation(
        evidence=evidence,
        reason_code=ReasonCode.EVIDENCE_SUFFICIENT_ON_TIME,
        reason_text="Structured result conforms to the supplier-exception schema.",
    )


def confidence_permits_resolution(
    call: NormalizedCall,
    settings: PolicySettings,
) -> tuple[bool, ReasonCode | None, str]:
    """Task-level gate on whether *any* recipient may resolve.

    Confidence is reported for the whole call task, not per recipient
    (``docs/provider-truth.md`` §6). It is applied as a dampener: it can
    hold a resolution back, but a confident batch never rescues a
    recipient whose own evidence is missing or ambiguous.

    Returns:
        ``(permitted, reason_code, reason_text)``. ``reason_code`` is
        ``None`` when permitted.
    """
    if call.task_completed is False:
        return (
            False,
            ReasonCode.TASK_NOT_COMPLETED,
            "CALL-E reports the call task did not reach a clear end state.",
        )

    if call.task_completed is None:
        return (
            False,
            ReasonCode.TASK_NOT_COMPLETED,
            "CALL-E has not published a terminal task-completion judgment.",
        )

    conf = call.completion_confidence
    if conf is None:
        return (
            False,
            ReasonCode.LOW_COMPLETION_CONFIDENCE,
            "CALL-E published no completion confidence for this call task.",
        )

    if conf.score < settings.min_completion_confidence:
        return (
            False,
            ReasonCode.LOW_COMPLETION_CONFIDENCE,
            (
                f"Completion confidence {conf.score:.2f} ({conf.label}) is below the "
                f"{settings.min_completion_confidence:.2f} threshold required to resolve "
                "a purchase order automatically."
            ),
        )

    return (True, None, f"Completion confidence {conf.score:.2f} ({conf.label}).")


def classify_outcome(evidence: SupplierEvidence) -> Outcome:
    """Map validated evidence onto the internal outcome vocabulary.

    ``docs/hackathon-build/prd.md`` §4. This is a *description* of what
    the supplier said, not yet a decision about what to do -- that is
    ``transition_policy.decide``.
    """
    if evidence.needs_human == "yes":
        return Outcome.BLOCKED_NEED_HUMAN
    if evidence.status == "blocked":
        return Outcome.BLOCKED_NEED_HUMAN
    if evidence.is_ambiguous:
        return Outcome.ANSWER_UNCLEAR
    if evidence.received == "yes" and evidence.status == "on_time":
        return Outcome.CONFIRMED_ON_TIME
    if evidence.received == "yes" and evidence.status == "delayed":
        return Outcome.CONFIRMED_DELAYED
    return Outcome.ANSWER_UNCLEAR
