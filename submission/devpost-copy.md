# Devpost submission — final copy

Target track: **Most Practical Use Case.** Ready to paste into the
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
React/TypeScript operations console on top, verified end to end by 307
tests running against real PostgreSQL, not SQLite.

## Challenges we ran into

- **The response schema evolved once real evidence forced it to.** The
  first version could represent a supplier's status but not *who
  actually answered the phone* or *why* a human was needed beyond one
  flag. We added `spoke_with` and `escalation_reason` to a v2 schema,
  then built a scenario to prove the gate actually holds: a supplier
  contact answers cleanly -- "yes, on time" -- but is the wrong person
  for POs. The exception still routes to a human, because an
  authorized phone *number* is not an authorized *person*.
- **A completion-confidence hazard, caught before it shipped.** A live
  call that never connected came back with `completion_confidence: 0.82,
  "high"` -- high confidence that the task did *not* complete. Treated
  naively as a positive signal, that number would have resolved a
  purchase order nobody discussed. See `submission/calle-feedback.md`.
- **An undocumented daily call cap.** Discovered mid-development via a
  live 429 response ("The 24-hour call plan limit has been reached"),
  not mentioned in the hackathon resources.
- **We shipped a real credential leak to ourselves, and our own safety
  net missed it.** A live CALL-E key ended up committed to
  `.env.example`. The pre-commit secret scanner should have caught it,
  but its "already committed" check used `git ls-files`, which returns
  nothing at all in a repository with zero commits -- so the check had
  been silently a no-op the entire time. Found it by re-auditing the
  scanner itself rather than trusting a string of green checks, fixed
  both the leak and the scanner, and added a regression test so that
  class of bug can't recur silently again.

## Accomplishments that we're proud of

Two real calls placed to an authorized number during development,
with both terminal responses committed as regression fixtures rather
than described from memory -- the system is checked against what CALL-E
actually returns, not only against our reading of the API contract.
307 tests passing, ruff and mypy clean, CI green on every push against
real PostgreSQL with no API key present, so automated runs can never
place a call.

## What we learned

Building against CALL-E meant designing for an evidence-producing API
rather than a synchronous one -- the call doesn't just succeed or fail,
it comes back with a confidence score, a transcript, and structured
claims that have to be independently re-validated rather than trusted.
That reframed the whole project: the hard part was never placing the
call, it was deciding how much to believe the answer.

## What's next for Resolve-E

- A recorded, approved live smoke test with an answered call, to verify
  the `structured_result` and `transcript_turns` mapping against a real
  conversation with a real answer (currently verified against a real
  *unanswered* call and one real but non-substantive answer).
- Operator authentication (currently a single-operator demo).
- The CALL-E Goals API as the production target once Goals become
  creatable via API rather than only via CALL-E Chat -- it publishes a
  closed error taxonomy (`no_answer`, `declined`, `result_invalid`, ...)
  that the Calls API's `failure_code` deliberately does not.

## Built with

`python` `fastapi` `postgresql` `sqlalchemy` `alembic` `redis` `react`
`typescript` `vite` `docker` `docker-compose` `call-e` `calle-ai`
`pytest` `ruff` `mypy` `github-actions` `pydantic` `psycopg`

## Try it out links

- GitHub: `https://github.com/Unknown1502/resolve-e-calle`
- Demo video: not yet public -- `resolve-e-demo.mp4` still needs to be
  uploaded somewhere (YouTube unlisted works) before Devpost's video
  field will take a link.

---

**Note:** README.md and this file previously said "299 tests" --
that was stale. Current count is **307**, confirmed by a fresh `pytest`
run. Re-confirm against a fresh run immediately before submitting if
any further work happens after this packet was written.
