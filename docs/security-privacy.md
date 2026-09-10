# Resolve-E Security and Privacy

## Scope

The MVP intentionally limits phone interactions to low-sensitivity operational facts.

## Data minimization

Do not send:

- payment card information;
- banking credentials;
- passwords;
- government identifiers;
- medical information;
- unnecessary personal details.

## Phone authorization

Every recipient number must come from a validated, authorized source.

The UI must not accept arbitrary numbers for autonomous calling in the production design.

## Disclosure budget

Before call creation, inspect the generated task for sensitive patterns and block unsafe content.

Inspect again after task generation because sensitive material can enter through free-form context.

The CALL-E community examples also emphasize treating phone calls as a weak identity factor and bounding disclosures before a conversation starts. Use those patterns as design inspiration.

## Human escalation

Immediately escalate when:

- a consequential decision is requested;
- a caller disputes the record;
- identity cannot be established sufficiently for the intended disclosure;
- the recipient requests a human;
- a sensitive topic appears.

## Secrets

- `CALLE_API_KEY` is server-side only;
- never expose it to the frontend;
- redact secrets in logs;
- use environment variables in local development;
- keep production secrets in the deployment platform's secret manager.

## Logging

Log operational metadata, not unnecessary transcript content.

Good:
```text
workflow_id=wr_123 call_id=call_123 decision=human_review
```

Avoid:
```text
full phone number + full transcript + unrelated personal information
```

## Implementation note: identity and escalation (added this pass)

Two lines above were originally aspirational and are now implemented as
structured, tested behaviour rather than prompt-only guidance:

**"Treat phone calls as a weak identity factor."** `spoke_with` records
who the recipient *claimed* to be (`intended_contact`,
`authorized_representative`, `wrong_person`, `unknown`) as reported by
the call itself -- it is not an authentication factor, and is never
described as one. It gates only whether an answer may *close* an
exception; combined with an already-authorized destination number, it
raises the bar from "we dialled a number we were told belongs to this
supplier" to "the person who answered stated they hold that role" --
appropriate for a call that discloses nothing beyond a supplier name and
PO number before the claim is made, and short of what any consequential
action would require. See `docs/provider-truth.md` §11.

**"Identity cannot be established sufficiently for the intended
disclosure" (escalation trigger).** Implemented as the identity gate in
`app.policies.transition_policy.decide()`: a recipient whose `spoke_with`
is `wrong_person` or `unknown` cannot resolve an exception, regardless of
how clear the rest of their answer is. Regression coverage:
`backend/tests/policies/test_decision_engine.py` and the seeded demo
case `PO-4827` (right number, wrong person).

**"A caller disputes the record" / commercial negotiation.** These are
now separately coded (`escalation_reason`: `disputes_po`,
`commercial_change`) rather than collapsed into one `needs_human`
signal, so an operator triaging `HUMAN_REVIEW` can see which applies
without reading the transcript first.

**Full transcripts are stored and shown in the operator detail view**
(`call_attempts.transcript`, `AttemptView.transcript`), which is a
departure from "log operational metadata, not unnecessary transcript
content" above -- that guidance is about *logs*, and no log statement in
this codebase includes transcript content (verified: no `log.*` call
references it). The detail view is the one place a human is expected to
read the evidence behind a decision, particularly a `HUMAN_REVIEW` from
the identity gate or an escalation; it is never included in the
exception list/summary view, never logged, and phone numbers remain
masked throughout.
