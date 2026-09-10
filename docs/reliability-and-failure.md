# Reliability and Failure Strategy

## Important expectation

No distributed phone workflow can honestly be promised to be “flawless.” The engineering target is **controlled failure**: duplicate actions are prevented, ambiguity is preserved, provider uncertainty is reconciled, and consequential actions fail closed.

## 1. Duplicate call prevention

Two distinct mechanisms are at work, and they guard against two distinct
failure modes. Neither can substitute for the other.

### 1a. Local row-locking — prevents two of *our own* dispatchers from overlapping

The API and the background worker both scan for callable exceptions and
can, in principle, do so at the same instant. Without a lock they can
select overlapping sets and plan *different batches that share a
supplier*. Because a batch's idempotency key is derived from its
membership (below), two overlapping-but-different batches produce two
*different* keys — so the provider has no way to recognise them as the
same work, and would place two real calls to one supplier.

Resolve-E prevents this with `SELECT ... FOR UPDATE SKIP LOCKED` held
from the eligibility check through the `CALL_PLANNED` transition
(`docs/architecture/low-level.md`: "database row locks for stateful
operations"). This is a **purely local, database-level guarantee** — it
constrains only how Resolve-E's own processes claim work, and is fully
verified: `backend/tests/services/test_concurrent_dispatch.py` races two
real PostgreSQL sessions against each other and asserts the claimed sets
never overlap.

### 1b. Provider idempotency key — covers a lost response after the provider already acted

Separately, CALL-E provides an `Idempotency-Key` header on call creation
(§9 of `docs/provider-truth.md`). This is what protects a *different*
failure: a single create request that reaches CALL-E and is accepted,
but whose HTTP response is then lost before Resolve-E sees the call id —
followed by a retry. Resolve-E derives one stable, content-based key per
logical batch and reuses that exact key on every retry of the same
logical create:

```text
resolve-e:batch:<sha256(sorted("{workflow_run_id}:{attempt_no}", …))[:32]>
```

Content-derived, not counter- or clock-derived, so a retry of the exact
same logical batch is byte-identical and reordering the batch does not
change it (`backend/tests/adapters/test_calle_contract.py::TestIdempotencyKey`).
Never generate a new key for the same logical create retry.

### What is, and is not, proven

Local row-locking is proven: raced, tested, green. The idempotency-key
guarantee for a *lost-response* retry is **provider-dependent and has
not been exercised live** — doing so would mean deliberately dropping a
real network response mid-call, which was not attempted. It rests on
CALL-E's documented behaviour ("reusing the same key with the same
request returns the original call instead of creating a duplicate"),
verified by two live calls to the extent that the header was accepted
and did not cause a rejection, and by our own mock double honouring it
by construction — which proves our *code* does the right thing, not that
the *provider* does. See `docs/provider-truth.md` §9 for the full
breakdown of what is reported, mock-verified, live-verified, and still
open.

## 2. Webhook duplication

Treat webhook delivery as at-least-once.

Use:

```text
provider_event_id UNIQUE
```

or, where necessary, a deterministic hash of the provider event.

Duplicate event:

```text
already processed? → no-op
```

## 3. Lost webhook

Never assume the webhook is the sole source of truth.

Use a reconciliation worker:

```text
active calls older than threshold
        ↓
GET /calls/{call_id}
        ↓
terminal?
        ↓
process terminal result
```

## 4. Provider timeout

A timeout during creation is dangerous because the request may have reached the provider.

Therefore:

```text
create timeout
    ↓
retry with SAME idempotency key
    ↓
provider returns existing logical call or creates it once
```

Do not retry with a new logical attempt number.

## 5. Structured result is null

CALL-E documents `structured_result = null` when it cannot produce a schema-valid result or when no result schema was provided.

Resolve-E must never interpret null as success.

Action:

```text
null
 ↓
reconcile call/events
 ↓
if still null:
    retry if safe
    otherwise human review
```

## 6. Low-confidence or ambiguous evidence

Do not convert uncertainty into a positive business outcome.

Examples:

- “I think we shipped it.”
- “Probably Friday.”
- “Someone else handles that.”
- “Yes, but I have not checked.”

These should remain unknown unless the required fact is clearly supported.

## 7. Concurrent operator change

Suppose the operator resolves the exception manually while a call is still in progress.

When the call returns:

```text
workflow version mismatch
        ↓
reload state
        ↓
if terminal:
    store call result as historical evidence
    do not overwrite terminal state
```

## 8. Retry budget

Recommended default:

- attempt 1: immediate;
- attempt 2: after policy delay;
- attempt 3: final bounded attempt;
- then human review.

The exact schedule is a policy setting, not an LLM decision.

## 9. Provider failure codes

The CALL-E API exposes failure code/message information. Do not branch business retry behavior on undocumented provider failure-code strings. Normalize raw failures into your own controlled retry taxonomy.

## 10. Auditability

For every final decision store:

- workflow ID;
- exception ID;
- attempt number;
- CALL-E call ID;
- terminal status;
- structured result;
- evidence;
- policy decision;
- timestamp;
- actor;
- previous state;
- new state.

## 11. CALL-E implementation notes

The current CALL-E docs show asynchronous call creation, strict structured result schemas, per-request webhook URLs, caller metadata, call retrieval, and event retrieval. They also document the importance of idempotency for safe create retries.

The official CALL-E troubleshooting documentation also notes environment-specific CLI issues and recommends verifying the available tools and testing shell/timeout behavior separately. Keep provider-specific failures isolated behind the adapter.
