# Resolve-E Decision Engine

## Core instruction

**Do not use an LLM to choose the final workflow state.**

The decision engine consumes validated structured evidence and deterministic context.

## Pseudocode

This is `app.policies.transition_policy.decide()`, current as of schema
`supplier-exception.v2`. `result` below is the recipient's evidence
*after* re-validation (`app.policies.evidence_policy`), never the raw
provider payload -- see "Result validity" below.

```text
function decide(exception, call, recipient):
    if exception.state in TERMINAL_STATES:
        return NOOP                            # a late result is history, never an overwrite

    if call.status in ["failed", "canceled"]:
        return RETRY if budget_available(exception) else HUMAN_REVIEW

    result = validate(recipient)                # re-validation, not the raw payload
    if result is invalid or recipient was never reached:
        return RECONCILE if reconciliation_available else
               (RETRY if budget_available(exception) else HUMAN_REVIEW)

    # -- escalation triggers, checked before any positive answer --
    if result.escalation_reason == "asked_not_to_be_called":
        suppress_future_calls(recipient)         # outlives this workflow
        return HUMAN_REVIEW
    if result.escalation_reason == "disputes_po":
        return HUMAN_REVIEW
    if result.escalation_reason == "commercial_change":
        return HUMAN_REVIEW
    if result.escalation_reason == "wants_human":
        return HUMAN_REVIEW
    if result.needs_human == "yes":
        return HUMAN_REVIEW

    # -- identity gate: an authorized NUMBER does not authorize whoever answers it --
    could_close = result.received == "yes" and result.status in ("on_time", "delayed")
    if could_close and result.spoke_with not in ("intended_contact", "authorized_representative"):
        return HUMAN_REVIEW                      # WRONG_PERSON or RECIPIENT_NOT_IDENTIFIED

    # -- confidence dampener: may withhold a resolution, never grant one --
    if could_close and not confidence_permits_resolution(call):
        return HUMAN_REVIEW

    if result.received == "yes" and result.status == "on_time":
        return RESOLVE_ON_TIME

    if result.status == "delayed":
        date = parse_ship_date(result.ship_date)  # None for anything not confidently parseable
        if date is None:
            return HUMAN_REVIEW
        if not delay_within_policy(exception, date):
            return HUMAN_REVIEW
        if result.received != "yes":
            return HUMAN_REVIEW                    # delayed without receipt is self-contradictory
        return RESOLVE_DELAYED

    if result.status == "blocked":
        return HUMAN_REVIEW

    if result.received == "no":
        return RETRY if budget_available(exception) else HUMAN_REVIEW

    return HUMAN_REVIEW                            # ambiguity is never resolved
```

## Guardrails

### Never resolve when

- `structured_result == null`, or fails re-validation (unknown enum
  value, wrong type, missing/extra field);
- the recipient was never reached (`status != completed`) -- an
  attached result describes nothing that happened;
- the identity gate fails: nobody confirmed they are the intended
  contact or an authorized representative (`spoke_with` is
  `wrong_person` or `unknown`) -- see "Identification" below;
- confidence is insufficient (`task_completed is False`, `None`, or
  `completion_confidence.score` below the policy threshold) -- this can
  only withhold a resolution the evidence would otherwise support,
  never grant one the evidence does not;
- a consequential commercial decision is requested
  (`escalation_reason == "commercial_change"`);
- the workflow has been concurrently modified (version conflict);
- the CALL-E call cannot be reconciled.

### Retry only when

- the previous action is known to be retryable (`ErrorClass` is in
  `RETRYABLE_ERROR_CLASSES` -- never inferred from a provider
  `failure_code` string, which has no published enum);
- the attempt budget permits it (`ATTEMPT_BUDGET_EXHAUSTED` is the only
  reason that ends retries; a backoff merely "not yet due" stays
  `RETRY_PENDING`, not `HUMAN_REVIEW` -- a scheduling delay is not a
  failure);
- the same logical attempt is not already dispatched and unresolved;
- the exception is not terminal.

### Human review triggers, distinctly coded

Each of these used to collapse into a single generic `needs_human`
signal. They are now separately represented, both in the schema and in
the reason code an operator sees:

| Trigger | `escalation_reason` / condition | Reason code |
|---|---|---|
| Asked not to be called again | `asked_not_to_be_called` | `ASKED_NOT_TO_BE_CALLED` (also suppresses future calls **for this exception** -- not yet phone-number-wide; see `docs/provider-truth.md` "Suppression scope") |
| Disputes the purchase order | `disputes_po` | `SUPPLIER_DISPUTES_PO` |
| Commercial/contract change requested | `commercial_change` | `COMMERCIAL_CHANGE_REQUESTED` |
| Asked for a person | `wants_human` or `needs_human == yes` | `SUPPLIER_REQUESTED_HUMAN` |
| Wrong person answered | `spoke_with == wrong_person` (on an otherwise-closeable answer) | `WRONG_PERSON` |
| Nobody's identity established | `spoke_with == unknown` (on an otherwise-closeable answer) | `RECIPIENT_NOT_IDENTIFIED` |
| Ambiguous status | `received` or `status` is `unknown` | `EVIDENCE_AMBIGUOUS` |
| Repeated failed calls | attempt budget exhausted | `ATTEMPT_BUDGET_EXHAUSTED` |
| Concurrent update conflict | version mismatch | `VERSION_CONFLICT` |
| Low completion confidence | below threshold | `LOW_COMPLETION_CONFIDENCE` |

### Identification: what this actually proves

`spoke_with` is **self-reported by the person on the call**, extracted
from what they said. It is not an authentication factor: it proves
someone claimed a role, not that the claim is true. Combined with an
authorized *destination number*, it raises the bar from "we dialled a
number we were told belongs to this supplier" to "we dialled that
number, and the person who answered stated they were the contact" --
which is the right, low-disclosure bar for a status-check call that
discloses nothing sensitive before that claim is made, and is far short
of what would be required before any consequential disclosure or
action. See `docs/security-privacy.md`.

## Example policy object

```json
{
  "max_attempts": 3,
  "retry_on": [
    "provider_timeout",
    "temporary_provider_failure",
    "recipient_unreachable"
  ],
  "never_retry_on": [
    "human_review_requested",
    "policy_block",
    "commercial_negotiation"
  ],
  "delay_threshold_hours": 48
}
```
