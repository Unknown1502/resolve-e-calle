# Resolve-E — Decision Log

Short, dated record of material decisions made while reconciling
documented intent against the implementation. Full reasoning for each
lives where cited; this is the index.

## A. Delayed resolution must require `received=yes`

**Finding:** already correct. `transition_policy.decide()` escalates to
`HUMAN_REVIEW` when `status=="delayed"` and `received!="yes"` (branch
10, "delayed without receipt is self-contradictory"), before any
resolution is considered.

**Decision:** no code change. Added
`test_delay_without_receipt_is_inconsistent_and_escalates` was already
present; confirmed it still passes and still tests this exact case.

## B. Provider field naming (`status` vs `po_status`)

**Finding:** already correct, and confirmed against real CALL-E bytes.
`status` is a reserved recipient-result field name; the schema sends
`po_status`, and the internal domain model keeps calling it `status`
with the rename happening only at the adapter boundary
(`evidence_policy.WIRE_TO_INTERNAL`).

**Decision:** no code change. `backend/tests/fixtures/real_call_no_answer.json`
independently confirms the field came back as `po_status`.

## C. Recipient identification

**Finding:** a real gap. The schema had no field to represent who was
reached; an authorized destination number was being treated as
sufficient to close an exception, which the blueprint's own guardrail
list ("the recipient is not appropriately identified") already
prohibited without a way to detect it.

**Decision:** add `spoke_with` (`intended_contact` /
`authorized_representative` / `wrong_person` / `unknown`) to schema v2.
Explicitly documented as self-reported, not authenticated -- appropriate
because the call discloses nothing before the claim is made, and the
outcome it gates is a routine status check, not a consequential action.
Gate only outcomes that would *close* the workflow; never gate `RETRY`
or a plain `HUMAN_REVIEW`, since those need no identity claim to be
correct. Full reasoning: `docs/provider-truth.md` §11,
`docs/security-privacy.md`.

## D. Escalation evidence

**Finding:** a real gap. `needs_human: yes` was the only signal; a
routine request for a person, a dispute, a commercial-change request,
and "don't call me again" were indistinguishable to an operator, and
none of them could trigger an actual consequence.

**Decision:** add `escalation_reason` (`none` / `wants_human` /
`disputes_po` / `commercial_change` / `asked_not_to_be_called` /
`unknown`) to schema v2, each mapped to its own `ReasonCode`.
`asked_not_to_be_called` additionally blocklists the recipient via
`result_processor.py`, so it has a real, tested consequence rather than
just a different label. No transcript-text search was introduced; the
gate reads only the structured, re-validated field. Full reasoning:
`docs/provider-truth.md` §11.

## E. Caller identity and disclosure

**Finding:** a real gap, confirmed by a live call. The blueprint already
required "identify yourself and the company you represent" as objective
item 1; the implemented task never actually said the agent was an AI or
named a company. `backend/tests/fixtures/real_call_connected.json`
shows the opening line was "Hi, am I speaking with the supplier contact
or an authorized representative?" -- no disclosure.

**Decision:** rewrite the task's opening instruction to require stating
AI status and a real, operator-configured `buyer_company`. Add
`Settings.buyer_company` / `disclosure_ready`; live dispatch **refuses**
(`PolicyViolation`) rather than substitutes a placeholder when unset,
because a placeholder would be spoken aloud to a real person. Workflow
IDs and other internal identifiers stay in `metadata`, never in spoken
text. Regression: `TestCallerDisclosure` (6 tests), `test_config.py` (7
tests, including a self-caught wrong-env-var-name bug).

## F. Provider capabilities

**Finding:** no gap requiring a code change, but the *verification
standard* around one claim was too generous. `docs/provider-truth.md`
§9 was titled "confirmed" for provider-side idempotency-key dedup on a
retry after a lost response, when what was actually verified was: the
header is accepted (live), our own key derivation is deterministic
(local), and our own mock double honours it (local, by construction --
proves our code, not the provider's).

**Decision:** no code change. Retitled and rewrote §9 to state plainly
what is reported vs. verified, and added `docs/reliability-and-failure.md`
§1a/§1b distinguishing the two independent guarantees (local row-locking,
fully proven by racing real sessions; provider-side dedup, documented
but not live-exercised). The residual limitation is explicit rather
than implied away.

Separately confirmed via the official SDK's own source
(`calle.calls.CalleCalls.create`): our hand-rolled HTTP client's request
shape is identical to the SDK's -- same six body fields, same
`Idempotency-Key` header, same endpoint. `CalleAPIError` was found to
carry `code`/`status_code`/`details` but **not** a `.message` attribute
(it is passed to `Exception.__init__` and readable only via `str()`);
an earlier draft this session assumed `.message` existed, mypy caught it
before it shipped.

## Also decided, outside A-F

- **Test hermeticity is structural, not a convention.** A session-scoped
  autouse fixture (`conftest.py::_never_place_a_real_call`) clears and
  asserts `live_calls_enabled is False` for the entire test session,
  regardless of what `.env` says. Found necessary because `.env` was
  discovered armed at the start of this session.
- **No docker rebuild/redeploy performed without approval.** The
  running containers (including a public tunnel) predate this session's
  code and are confirmed stale (`AttributeError` on `.disclosure_ready`).
  Left as found; flagged in `docs/verification-report.md` as a decision
  for the user.
- **Blueprint prompt docs are preserved, not rewritten.** Consistent
  with this repository's own established pattern
  (`docs/provider-truth.md`'s stated policy: "where a blueprint document
  and this document disagree, this document wins, and the blueprint is
  wrong" -- as a *note*, not a silent edit). `docs/prompts/resolve-e-system.md`
  and `docs/prompts/call-task-template.md` keep their original text and
  gained a short, clearly-labelled addendum pointing at what changed and
  why, rather than being rewritten to describe current behaviour as if
  it were the original design.
- **Date handling (out of A-F, checked because the task explicitly asked):**
  `ship_date.parse_ship_date()` already anchors "today" to
  `datetime.now(UTC).date()`, injectable and tested; resolves bare
  weekday names via unambiguous next-occurrence arithmetic (an explicit,
  defensible normalization method, not a guess); and refuses ambiguous
  numeric forms like `09/11` outright. No change needed -- this already
  satisfies the instruction that vague dates get clarification-or-refusal
  unless an explicit method exists, and weekday-name resolution is that
  method.
