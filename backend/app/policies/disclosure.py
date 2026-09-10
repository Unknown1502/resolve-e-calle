"""Disclosure budget: what may be said on the phone.

``docs/security-privacy.md``:

    Before call creation, inspect the generated task for sensitive
    patterns and block unsafe content. Inspect again after task
    generation because sensitive material can enter through free-form
    context.

That second sentence is the point. Supplier names, contact names and PO
numbers are operator-supplied free text, and they are interpolated into
the task. A card number pasted into a "supplier name" field would
otherwise be read aloud to whoever answers the phone.

So the scan runs on the *rendered* task, immediately before dispatch --
not on the template, and not on the fields individually.

This is a fail-closed gate. A match blocks the call and escalates; it
never redacts and proceeds, because a redaction that silently removes
half a sentence can change what the agent asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import Decision, ReasonCode
from app.domain.models import PolicyDecision


@dataclass(frozen=True, slots=True)
class SensitivePattern:
    name: str
    pattern: re.Pattern[str]
    explanation: str


def _luhn_valid(digits: str) -> bool:
    """Card-number checksum, so ordinary long numbers do not trip the gate.

    A 16-digit part number is not a payment card. Requiring the checksum
    keeps this rule precise enough to stay switched on.
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_PATTERNS: tuple[SensitivePattern, ...] = (
    SensitivePattern(
        "credential",
        re.compile(
            r"\b(?:password|passcode|pass\s?word|pin\s?(?:code|number)?|otp|"
            r"one[- ]time[- ]code|api[_ -]?key|secret|token|credential)s?\b",
            re.IGNORECASE,
        ),
        "credential or authentication material",
    ),
    SensitivePattern(
        "bank_account",
        re.compile(
            r"\b(?:iban|swift|bic|sort\s?code|routing\s?(?:number|no)|"
            r"account\s?(?:number|no)\b|ifsc|upi\s?id)\b",
            re.IGNORECASE,
        ),
        "bank or payment routing details",
    ),
    SensitivePattern(
        "government_id",
        re.compile(
            r"\b(?:ssn|social\s?security|aadhaar|aadhar|passport\s?(?:number|no)|"
            r"national\s?insurance|nino|tax\s?id|pan\s?card)\b",
            re.IGNORECASE,
        ),
        "government identifier",
    ),
    SensitivePattern(
        "us_ssn_literal",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "a literal US social security number",
    ),
    SensitivePattern(
        "medical",
        re.compile(
            r"\b(?:diagnosis|prescription|medical\s?record|patient\s?(?:id|record)|"
            r"health\s?insurance)\b",
            re.IGNORECASE,
        ),
        "medical information",
    ),
    SensitivePattern(
        "commercial_commitment",
        re.compile(
            r"\b(?:negotiate|discount|rebate|price\s?(?:change|increase|reduction)|"
            r"payment\s?terms|contract\s?(?:amendment|change)|penalt(?:y|ies)|"
            r"purchase\s?price|renegotiat)\w*\b",
            re.IGNORECASE,
        ),
        "a commercial negotiation the agent is not authorized to conduct",
    ),
)

_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass(frozen=True, slots=True)
class DisclosureFinding:
    pattern_name: str
    explanation: str
    #: The matched text is deliberately NOT stored. Persisting it would
    #: copy the sensitive value into the audit log we are protecting.
    match_offset: int


def scan_task_text(task_text: str) -> list[DisclosureFinding]:
    """Return every sensitive pattern found in a rendered call task."""
    findings: list[DisclosureFinding] = []

    for sp in _PATTERNS:
        if m := sp.pattern.search(task_text):
            findings.append(DisclosureFinding(sp.name, sp.explanation, m.start()))

    for m in _CARD_CANDIDATE.finditer(task_text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            findings.append(
                DisclosureFinding("payment_card", "a payment card number", m.start())
            )
            break

    return findings


def check_disclosure_budget(task_text: str) -> PolicyDecision:
    """Fail closed if the rendered task carries anything it must not say.

    Returns :attr:`Decision.PROCEED_WITH_CALL` when clean, otherwise
    :attr:`Decision.HUMAN_REVIEW` -- never a silently redacted call.
    """
    findings = scan_task_text(task_text)
    snapshot = {
        "task_length": len(task_text),
        "finding_count": len(findings),
        # Names only. The matched values never enter the audit trail.
        "findings": [f.pattern_name for f in findings],
    }

    if findings:
        explanations = ", ".join(dict.fromkeys(f.explanation for f in findings))
        return PolicyDecision(
            decision=Decision.HUMAN_REVIEW,
            reason_code=ReasonCode.SENSITIVE_CONTENT_IN_TASK,
            reason_text=(
                f"Call task blocked before dispatch: it contains {explanations}. "
                "A human must review this exception."
            ),
            input_snapshot=snapshot,
        )

    return PolicyDecision(
        decision=Decision.PROCEED_WITH_CALL,
        reason_code=ReasonCode.TASK_WITHIN_DISCLOSURE_BUDGET,
        reason_text="Rendered call task contains no prohibited content.",
        input_snapshot=snapshot,
    )
