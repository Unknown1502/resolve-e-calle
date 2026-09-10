# Provider Truth — CALL-E Developer API

**Source of record:** `https://docs.heycall-e.com/openapi/calle.openapi.yaml`
(`CALL-E Developer API`, `version: 0.7.0`), retrieved 2026-09-09.

This document exists because the rest of `docs/` was written *before* the
OpenAPI contract was read. Where a blueprint document and this document
disagree, **this document wins**, and the blueprint is wrong.

The master coding-agent prompt permits exactly this:

> Do not invent a different architecture unless a documented provider
> constraint makes it necessary.

Every delta below is a documented provider constraint. Nothing here is a
preference.

---

## 1. Base URL, auth, and surfaces

```text
servers:  https://api.heycall-e.com
security: bearerAuth   →   Authorization: Bearer $CALLE_API_KEY
```

Two independent execution surfaces exist:

| Surface | Endpoints | Creatable via API? |
|---|---|---|
| **Calls** | `POST /v1/calls`, `GET /v1/calls/{call_id}`, `GET /v1/calls/{call_id}/events` | Yes |
| **Goals** | `GET /v1/goals`, `GET /v1/goals/{id}`, `POST /v1/goals/{id}/runs`, `GET /v1/goals/{id}/runs/{run_id}` | **No** — a Goal is authored in CALL-E Chat and published; the API only lists and runs them |

Resolve-E uses **Calls**. Goals cannot be created from code, so a judge
cloning this repository could not reproduce a Goal-based demo. See §8 for
why Goals are still the right production target.

## 2. Create Call — the actual request body

`CreateCallRequest` (`additionalProperties: false`, only `task` required):

```jsonc
{
  "task": "…",                     // string, minLength 1 — REQUIRED
  "recipients": [                   // array | null
    { "phones": ["+15550001111"],   // REQUIRED within a recipient, E.164
      "locale": "en-US",            // string | null, BCP 47
      "region": "US" }              // string | null
  ],
  "result_schema": { … },           // object | null — whole-task extraction
  "recipient_result_schema": { … }, // object | null — per-recipient extraction
  "metadata": { … },                // object, caller-owned, echoed on webhooks
  "webhook_url": "https://…"        // string(uri), per-request, in ADDITION
                                    //   to project-level delivery
}
```

**Delta vs `spec.md` §3.** The blueprint's call command is not a valid
request. It sends a flat `recipient_phone_e164`, and it sends
`idempotency_key` *in the body*. Corrected:

- recipients are a **list of objects**, each with a **list of phones**;
- `Idempotency-Key` is an **HTTP header**, `minLength 1`, `maxLength 255`;
- `attempt_no`, `workflow_run_id`, `exception_id` belong in `metadata`,
  which is the documented mechanism for caller correlation keys.

E.164 is enforced by the provider with `^\+[1-9]\d{6,14}$`. Resolve-E
validates against the same pattern before dispatch so a bad number fails
in our validation layer with an auditable reason rather than as an opaque
provider 400.

## 3. Two result schemas, not one

This is the single most consequential correction.

| Field | Extracted for | Appears on |
|---|---|---|
| `result_schema` | the **whole call task** | `CallTask.structured_result` |
| `recipient_result_schema` | **each recipient independently** | `CallTask.recipients[i].structured_result` |

`recipient_result_schema` is documented as being "useful for batch calls
where each recipient needs their own outcome."

Resolve-E therefore places the five-field supplier schema from
`spec.md` §4 — `received`, `status`, `ship_date`, `blocker`,
`needs_human` — at the **recipient** level, unchanged, and adds a small
task-level rollup. One CALL-E call task can then resolve N purchase
orders, each with its own evidence and its own deterministic decision.

**Reserved recipient field names.** The spec forbids using `summary`,
`status`, `transcript`, `call_id`, or timing fields as custom recipient
result fields. Our schema uses `status`.

> Resolution: the recipient schema field is named **`po_status`**, not
> `status`. The internal domain model still calls it `status`; the
> adapter renames on the boundary. This is recorded in
> `adapters/calle/schemas.py` and asserted by a test, because it is the
> kind of detail that silently returns `null` results if regressed.

## 4. Result-schema feature restrictions

Supported: `type`, `properties`, `required`, `enum`, nested `object`,
simple `array.items`, `description`, `additionalProperties: false`.

**Unsupported:** `$ref`, `oneOf`, `anyOf`, `allOf`, recursive schemas,
complex `format` validation, and `additionalProperties: true`.

Consequence: schemas must be emitted as literal, flat, self-contained
objects. Pydantic's `model_json_schema()` emits `$ref`/`$defs` for nested
models and **must not** be shipped to CALL-E unmodified. Resolve-E keeps
hand-written schema literals and asserts their shape in
`tests/adapters/test_result_schema_contract.py`.

The docs also warn that `description` values guide extraction but are
**not** hard validation:

> Hard validation comes from `type`, `required`, `enum`, and
> `additionalProperties`.

This is the provider stating, in its own documentation, the blueprint's
"LLM output is evidence, not authority" principle. Application-side
re-validation is mandatory, not defensive.

## 5. Terminal states, and the third webhook event

```text
CallStatus:      queued · in_progress · completed · failed · canceled
RecipientStatus: pending · in_progress · completed · failed · skipped
AttemptStatus:   queued · dialing · in_progress · completed · failed · canceled
```

`in_progress` explicitly **includes post-call result finalization**;
terminal states are published only once the post-call outcome exists.

Webhook event types:

```text
call.completed
call.failed
call.result_validation_failed      ← never mentioned in the blueprint
```

**Delta vs `reliability-and-failure.md` §5.** The blueprint models
extraction failure solely as `structured_result == null`. There is a
dedicated terminal event for it. Resolve-E maps
`call.result_validation_failed` to the `STRUCTURED_RESULT` error class
directly, which is both faster and more precise than inferring it from a
null field.

Webhook envelope:

```jsonc
{ "id": "evt_…",          // unique event id — the dedupe key
  "type": "call.completed",
  "created_at": "…",
  "data": { …full CallTask snapshot… } }   // data.id is the call_ id
```

Plus a **required** request header `CALL-E-Event-Id: ^evt_[A-Za-z0-9_-]+$`.
The docs instruct storing it *before* side effects. The blueprint's
`webhook_events.provider_event_id UNIQUE` design is correct as written;
Resolve-E persists the header and the body `id` and asserts they agree.

Any 2xx is treated as delivered and the response body is ignored.

## 6. Evidence fields the blueprint ignores

`CallTask` carries three terminal-evidence fields that appear nowhere in
the blueprint's decision table:

| Field | Type | Meaning |
|---|---|---|
| `task_completed` | `boolean \| null` | post-summary judgment that the task reached a clear end state; `null` until terminal |
| `completion_confidence` | `{score: 0..1, label} \| null` | confidence *in that judgment* |
| `evidence` | `string[]` | short evidence items supporting the outcome |

**These are task-level, not recipient-level.** `CallTaskRecipient`
carries only `status`, `structured_result`, `summary`, and `attempts`.

Resolve-E therefore treats recipient evidence as **primary** and
task-level confidence as a **dampener**: a low-confidence batch cannot
promote a recipient to a resolved state, but a high-confidence batch can
never rescue a recipient whose own structured result is absent or
ambiguous. Confidence can only ever *withhold* resolution, never grant
it. See `policies/evidence_policy.py`.

## 7. Failure codes are diagnostic, not branchable

```text
failure_code: "Diagnostic failure reason when status is failed;
               otherwise null. No published enum.
               … do not branch retry, reporting, or analytics logic
               on specific values."
```

`reliability-and-failure.md` §9 already says this. Resolve-E persists
`failure_code` and `failure_message` verbatim for support and displays
them in the UI, and derives retry behaviour **only** from
`CallStatus`, `RecipientStatus`, `AttemptStatus`, and the webhook event
type — all of which are published enums.

## 8. The Goals API publishes the taxonomy Calls does not

`GoalRunError.code` is a **closed enum**:

```text
call_failed · no_answer · declined · timed_out
result_invalid · result_unavailable · result_failed · canceled
```

This is precisely the normalized retry taxonomy Resolve-E has to invent
for itself on the Calls surface. Goal Runs additionally make
`Idempotency-Key` **required**, business-stable, and enforced:

> Derive it from a durable workflow event, for example
> `delivery:ORD-8472:confirm-window:v1` … changing the phone or
> variables while reusing the key returns `409 idempotency_conflict`.

That is the blueprint's idempotency doctrine, promoted into the provider
contract. It is the correct production target for Resolve-E, and the
adapter interface is shaped so a `GoalsCallProvider` can be added
without touching the policy engine or the state machine.

It is **not** the hackathon path, for one disqualifying reason: Goals
cannot be created through the API, so the demo would not be reproducible
from a clone.

## 9. Idempotency semantics, as documented — and what remains unverified

```text
Idempotency-Key (header, optional on POST /v1/calls, 1..255 chars):
  "Stable caller-provided key used to make create-call retries safe.
   Reusing the same key with the same request returns the original
   call instead of creating a duplicate."
```

The blueprint's rule — retry the same logical create with the *same*
key, never a new one — is exactly right. Resolve-E's key is batch-scoped
and content-derived so that a retry of the same logical batch is
byte-identical:

```text
resolve-e:batch:<sha256(sorted("{workflow_run_id}:{attempt_no}", …))[:32]>
```

Length is bounded at 46 characters, well inside the 255 limit.

**What is actually verified here, and what is not.** Two live calls
confirmed the header is accepted and does not cause a request rejection
(`Idempotency-Key` present, both calls created successfully). Local
tests confirm our own key derivation is deterministic and stable under
reordering, and that our mock double honours it by construction.

None of that tests the case the key exists to cover: a create request
that reaches CALL-E, is accepted, and whose *response is then lost*
before we see the call id — followed by a retry with the same key. That
would require deliberately dropping a live network response mid-call,
which was not attempted (and should not be, outside a controlled test
environment CALL-E offers for it). The guarantee that such a retry
returns the original call rather than a second one rests entirely on
CALL-E's server-side handling of the header, as documented. Resolve-E's
own row-locking (see `docs/reliability-and-failure.md` §1a) prevents a
different failure mode — two of *our own* dispatchers planning
overlapping batches — and does not, and cannot, substitute for this.

## 10. Delta summary

| # | Blueprint says | Provider says | Resolve-E does |
|---|---|---|---|
| 1 | `recipient_phone_e164` in body | `recipients[].phones[]` | Corrected shape |
| 2 | `idempotency_key` in body | `Idempotency-Key` header | Header |
| 3 | one `result_schema` | task **and** recipient schemas | Supplier schema at recipient level; rollup at task level |
| 4 | — | `status` is reserved per-recipient | Renamed to `po_status` on the wire |
| 5 | — | no `$ref`/`oneOf`/`anyOf`/`allOf` | Literal flat schemas, contract-tested |
| 6 | 2 webhook events implied | 3, incl. `call.result_validation_failed` | Mapped explicitly |
| 7 | decision on structured result only | `task_completed`, `completion_confidence`, `evidence` exist | Confidence dampener, withhold-only |
| 8 | — | Calls `failure_code` has no enum; Goals does | Never branch on `failure_code`; Goals noted as production path |
| 9 | one call = one exception | `recipients[]` is a batch | One call task resolves N POs independently |
| 10 | five evidence fields; identity and escalation implied, not represented | -- | `spoke_with` and `escalation_reason` added (schema v2); see §11 |

## 11. Schema evolution: identification and escalation (v1 -> v2)

This delta is not a provider correction like §§1-9 above -- CALL-E did
not change. It is a gap between what `docs/prompts/resolve-e-system.md`
and `docs/prompts/decision-engine.md` always *asked for* and what the
original five-field schema could actually *represent*.

Both blueprint documents already state the requirement:

> If the wrong person answers, disclose no unnecessary order
> information. -- `resolve-e-system.md`

> the recipient is not appropriately identified -- listed as a "never
> resolve when" guardrail in `decision-engine.md`

> supplier disputes the PO; commercial negotiation requested --
> listed as separate "Human review triggers" in `decision-engine.md`

The original `received` / `status` / `ship_date` / `blocker` /
`needs_human` schema had no field to carry *who was actually reached*,
and collapsed "wants a person", "disputes the order", "wants to
renegotiate", and "asked not to be called again" into a single
`needs_human: yes`. An operator triaging the queue could not tell those
apart, and nothing could act on a request not to be called again -- it
would simply be retried next cycle like any other `needs_human` case.

**What changed (`SCHEMA_VERSION = "supplier-exception.v2"`):**

- `spoke_with` -- `intended_contact` / `authorized_representative` /
  `wrong_person` / `unknown`. Self-reported, not authenticated: it
  proves someone made a claim about their role, not that the claim is
  true. Gates every outcome that would *close* the exception -- a
  clean "yes, on time" from `wrong_person` or `unknown` still escalates.
  It never gates `RETRY` or a straightforward `HUMAN_REVIEW`, since
  those need no identity claim to be correct.
- `escalation_reason` -- `none` / `wants_human` / `disputes_po` /
  `commercial_change` / `asked_not_to_be_called` / `unknown`, each
  mapped to its own `ReasonCode` (`SUPPLIER_REQUESTED_HUMAN`,
  `SUPPLIER_DISPUTES_PO`, `COMMERCIAL_CHANGE_REQUESTED`,
  `ASKED_NOT_TO_BE_CALLED`). The last one has a consequence beyond the
  one call: the orchestrator blocklists that recipient, so a later
  retry -- on this exception or any other for the same contact --
  cannot dial them back.

**What this does not claim.** `spoke_with` is exactly as weak an
identity signal as it sounds: a claim, not a credential. It is
appropriate here because the call itself discloses nothing beyond a
supplier name and a PO number *before* the claim is made (see
"Bounding the call" in the skill's `references/examples.md`), and
because the outcome it gates is a routine status update, not a
consequential action. See `docs/security-privacy.md` and the
caller-disclosure note below.

**Caller disclosure, corrected alongside it.** The original task
template (`docs/prompts/resolve-e-system.md`, `call-task-template.md`)
states the *intent* -- "Identify yourself and the company you
represent" is item 1 of the allowed objective -- but the implemented
task text never actually said the agent was an AI, or named a real
company, until this pass. A live call placed during development
confirmed the gap directly: the transcript opened with "Hi, am I
speaking with the supplier contact or an authorized representative?"
and never disclosed AI status or a company name (captured in
`backend/tests/fixtures/real_call_connected.json`). The task template
now opens by stating AI status and a configured `buyer_company`, and
live dispatch is refused -- not defaulted to a placeholder -- when no
company is configured (`Settings.disclosure_ready`,
`app/services/call_orchestrator.py`). Regression coverage:
`backend/tests/adapters/test_calle_contract.py::TestCallerDisclosure`.

Full current schema: `skills/exception-resolution-calls/references/result-schema.json`
(regenerated from the live schema, never hand-copied). Full branch
precedence: `docs/prompts/decision-engine.md` and
`skills/exception-resolution-calls/references/decision-table.md`.

### Suppression scope, stated precisely (found during release verification)

`asked_not_to_be_called` sets `ExceptionRecord.recipient_blocklisted` --
one row per purchase order. **It protects that exception only.** A
supplier who asks not to be called about PO-4827 is not called about
PO-4827 again; a different, later exception for the same phone number
(a new PO to the same contact) is unaffected and dispatches normally.
Several places in this documentation set previously described the
consequence as suppressing "this contact" or "the contact" without that
qualifier, which overstated the guarantee -- corrected across the docs
alongside this note. Proven, not assumed:
`backend/tests/services/test_reliability.py::test_suppression_is_scoped_to_the_exception_not_the_phone_number`
seeds two purchase orders on the same phone number, suppresses one, and
confirms the second still dispatches.

A phone-number-level "do not call" registry -- suppressing every open
and future exception for that number, not just the one being processed
-- is the natural next step and is not implemented. It would need a
lookup keyed by `recipient_phone_e164` at eligibility time rather than a
flag on one exception row.
