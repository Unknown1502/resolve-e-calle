# Devpost submission — draft copy

Target track: **Most Practical Use Case.** Fill in and paste into the
Devpost form; nothing here is submitted automatically.

## Inspiration

A business workflow doesn't stop because software is missing a field.
It stops because the next step is a phone call, and nobody has picked
up the phone. Overdue supplier purchase-order acknowledgements are the
first, clearest case of that: the PO, the supplier, and the missed
deadline are all already in the system. What's missing is one phone
call and a decision about what the answer means.

## What it does

Resolve-E detects an overdue purchase-order acknowledgement, checks
that a call is permitted, dispatches one CALL-E call task (covering
several suppliers at once via `recipients[]` and
`recipient_result_schema`), and extracts a strict, per-supplier
structured result. A deterministic policy engine -- not a language
model -- decides whether the exception resolves, retries, or goes to a
human, and why. Every decision carries a machine-readable reason code
and an audit trail an operator can actually read.

## How we built it

FastAPI + PostgreSQL backend with a pure state machine, four
deterministic policy modules, a transactional outbox, and three worker
loops (scanner, outbox, reconciliation). The official `calle-ai` Python
SDK is the live call transport (imported and called at runtime), with a
hand-rolled HTTP client kept as a verified-identical fallback -- its
request shape was checked line-for-line against the SDK's own source.
React/TypeScript operations console. [PLACEHOLDER: test count, e.g.
"299 tests" -- confirm the number is current before pasting].

## Challenges we ran into

- **The response schema evolved once real evidence forced it to.** The
  first version could represent a supplier's status but not *who
  actually answered the phone* or *why* a human was needed beyond one
  flag. A live call reaching the wrong desk with a confident "yes, on
  time" made the gap concrete: an authorized phone number is not an
  authorized person. [FILL IN: describe PO-4827 / the identity gate
  briefly if space allows]
- **A completion-confidence hazard, caught before it shipped.** A live
  call that never connected came back with `completion_confidence: 0.82,
  "high"` -- high confidence that the task did *not* complete. Treated
  naively as a positive signal, that number would have resolved a
  purchase order nobody discussed. See `submission/calle-feedback.md`.
- **An undocumented daily call cap.** Discovered mid-development via a
  live 429 response ("The 24-hour call plan limit has been reached"),
  not mentioned in the hackathon resources.

## Accomplishments we're proud of

Two real calls placed to an authorized number during development,
with both terminal responses committed as regression fixtures rather
than described from memory -- the system is checked against what CALL-E
actually returns, not only against our reading of the API contract.
[FILL IN once final: N tests passing, ruff/mypy clean.]

## What we learned

[FILL IN -- personal to the builder; e.g., what surprised you about
building against an async, evidence-producing phone API rather than a
synchronous one.]

## What's next for Resolve-E

- A recorded, approved live smoke test with an answered call, to verify
  the `structured_result` and `transcript_turns` mapping against a real
  conversation (currently verified against a real *unanswered* call
  only).
- Operator authentication (currently a single-operator demo).
- The CALL-E Goals API as the production target once Goals become
  creatable via API rather than only via CALL-E Chat -- it publishes a
  closed error taxonomy (`no_answer`, `declined`, `result_invalid`, ...)
  that the Calls API's `failure_code` deliberately does not.

---

**Before pasting:** every `[FILL IN]` / `[PLACEHOLDER]` above needs a
factual answer from the user -- these are not guessed to keep the draft
honest. Confirm the test count and any claimed number against a fresh
`pytest` run immediately before submitting, since the number will have
moved if any further work happens after this packet was written.
