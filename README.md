# Resolve-E

**An autonomous exception-resolution agent that uses phone calls as its execution layer.**

Built on [CALL-E](https://heycall-e.com) for the *CALL-E: Your Code Is Calling* hackathon.

```
299 tests  ·  1 CALL-E call task resolves N purchase orders  ·  0 duplicate calls  ·  Apache-2.0
```

---

## The problem

Software already knows which purchase orders are stuck. It has the PO, the
supplier, the SLA, the missed acknowledgement. What it cannot do is the last
step: someone has to pick up the phone and ask.

So the work piles up in a queue that a person drains by hand, one call at a
time, and the workflow sits blocked in between.

Resolve-E closes that loop. It detects the exception, checks that a call is
permitted, asks CALL-E to make the call, extracts structured evidence from what
the supplier actually said, and then **deterministic policy** — not a language
model — decides whether the order is resolved, needs retrying, or needs a person.

```
Detect exception → Validate policy → Call via CALL-E → Capture evidence
      → Validate evidence → Advance / retry / escalate
```

## What makes it different

**The language model is a witness, not a judge.** CALL-E holds the conversation
and extracts a strict JSON result. Every business decision after that is made by
a pure function in [`transition_policy.py`](backend/app/policies/transition_policy.py)
that no model participates in. That is the whole thesis: an agent you can let
near a real workflow is one whose decisions you can read.

**Unknown is a first-class result.** Most automations force a yes or a no.
Resolve-E has a third answer, and it is a peer of the other two — not an error
state. When a supplier says *"I believe someone in logistics has it,"* the
correct outcome is to hand it to a person, and the dashboard shows that as a
successful run of the agent, not a failure.

**One call task, many purchase orders.** CALL-E's `recipients[]` plus
`recipient_result_schema` lets a single call task carry several suppliers, each
with its own extracted result. Resolve-E fans that back out into independent
policy decisions — one click, one call task, seven distinctly-reasoned
outcomes, and no supplier's answer can influence another's.

**An authorized number is not an authorized person.** A supplier contact's
phone number being on file doesn't mean whoever picks it up is allowed to speak
for them. Resolve-E asks the agent to record who it actually reached
(`spoke_with`, self-reported and clearly documented as such — see
[`docs/security-privacy.md`](docs/security-privacy.md)) and gates any outcome
that would *close* an exception on it. A clean "yes, on time" from the wrong
desk still goes to a human.

## Try it

```bash
docker compose up --build          # migrates, seeds, and serves on :8000
```

Open <http://localhost:8000>, click **Call N suppliers**, then open any purchase
order to see the whole chain: the CALL-E call id, what the supplier said, the
policy decision, and the audit trail.

**No real phone call is placed.** `CALLE_LIVE_CALLS` defaults to `false`, and
the system runs against a deterministic simulator that emits payloads in the
exact shape of CALL-E's `CallTask` schema. To place real calls:

```bash
cp .env.example .env
# set CALLE_API_KEY, CALLE_LIVE_CALLS=true, CALLE_ALLOWED_NUMBERS to a
# number you are authorized to call, and BUYER_COMPANY to a real
# company name -- the agent refuses to dial without one, because it
# states that name aloud on the call
docker compose up --build
```

One real call, without the rest of the system:

```bash
resolve-e smoke-test +15550001111
resolve-e get-call call_...
```

### Local development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d postgres redis
resolve-e reset                                    # create tables + seed
uvicorn app.main:app --reload                      # API on :8000
python -m app.workers.runner                       # scanner, outbox, reconciler
cd frontend && npm install && npm run dev          # dashboard on :5173
pytest                                             # 299 tests
```

## What the demo shows

One click dispatches **one** CALL-E call task covering seven suppliers (an
eighth is not yet due, and stays uncalled). Each reaches a different, distinctly
reasoned outcome — every one a branch of the decision table in
[`docs/prompts/decision-engine.md`](docs/prompts/decision-engine.md):

| PO | What the supplier said | Outcome | Reason code |
|---|---|---|---|
| PO-4821 | "Yes, we have it. Ships Friday." | `RESOLVED_ON_TIME` | `EVIDENCE_SUFFICIENT_ON_TIME` |
| PO-4822 | "Received it, inventory delay, ships tomorrow." | `RESOLVED_DELAYED` | `EVIDENCE_SUFFICIENT_DELAYED` |
| PO-4823 | "I believe someone in logistics has it." | `HUMAN_REVIEW` | `EVIDENCE_AMBIGUOUS` — second-hand and hedged, never guessed |
| PO-4824 | "We can ship tomorrow if you raise the price." | `HUMAN_REVIEW` | `COMMERCIAL_CHANGE_REQUESTED` — distinctly coded, not generic `needs_human` |
| PO-4825 | *never answered* | `RETRY_PENDING` | `RECIPIENT_NOT_REACHED` — no conversation, so no evidence |
| PO-4826 | *inaudible; no schema-valid result* | `RETRY_PENDING` | `STRUCTURED_RESULT_MISSING` — null is never success |
| PO-4827 | "I'm not the right person for POs." *(but says on-time when asked)* | `HUMAN_REVIEW` | `WRONG_PERSON` — an authorized number is not an authorized person; a clean answer from the wrong contact still can't close it |
| PO-4830 | *not called* | `OPEN` | Acknowledgement is not due yet |

Every state and reason code in this table was checked by running the actual
`decide()` function against the seeded scenarios (`docs/demo.md`), not written
from memory.

## Architecture

```
Browser ──► Resolve-E API ──► Policy engine (pure, deterministic)
                │                    │
                │              State machine ──► PostgreSQL
                │                                    ▲
                └──► Outbox ──► Worker ──► CALL-E adapter ──► CALL-E ──► supplier
                                   ▲                             │
                                   └──── reconcile ◄── webhook ◄──┘
```

The CALL-E API key lives only in the backend process. The browser talks to
Resolve-E and never to CALL-E.

**The webhook is not the decision engine.** It validates shape, dedupes on the
provider event id, writes an outbox row, and returns 2xx — nothing else. The
decision happens in the reconciliation worker, against state fetched from
CALL-E. That is why a duplicate delivery is harmless and a *lost* delivery is
only a delay: the reconciliation sweep finishes any call whose webhook never
arrived, so the system is correct with no webhook configured at all.

| Layer | Where |
|---|---|
| Decision engine | [`policies/transition_policy.py`](backend/app/policies/transition_policy.py) |
| Evidence re-validation | [`policies/evidence_policy.py`](backend/app/policies/evidence_policy.py) |
| State machine | [`domain/state_machine.py`](backend/app/domain/state_machine.py) |
| CALL-E client | [`adapters/calle/client.py`](backend/app/adapters/calle/client.py) |
| Wire schemas + task text | [`adapters/calle/schemas.py`](backend/app/adapters/calle/schemas.py) |
| Batch orchestration | [`services/call_orchestrator.py`](backend/app/services/call_orchestrator.py) |
| Fan-out to N decisions | [`services/result_processor.py`](backend/app/services/result_processor.py) |

## How CALL-E is used

Resolve-E uses the **official `calle-ai` SDK** as its default live transport —
`from calle import CalleClient`, called at runtime — and keeps a hand-rolled
HTTP client as a fallback. Both build byte-identical requests, which is the
closest available check on whether our reading of the API is right rather than
merely self-consistent. Swapping between them touches no policy code, because
all three transports (SDK, HTTP, simulator) satisfy one runtime-checkable
`CallProvider` protocol.

It calls `POST /v1/calls`, `GET /v1/calls/{id}` and `GET /v1/calls/{id}/events`,
and uses these provider features deliberately:

- **`recipients[]`** — one call task per batch of suppliers.
- **`recipient_result_schema`** — an independent strict-JSON result per supplier,
  now including `spoke_with` (who was actually reached, self-reported — see
  `docs/security-privacy.md`) and `escalation_reason` (distinctly coded, so
  a routine request for a human, a dispute, a commercial ask, and "don't call
  me again" are never collapsed into one flag).
- **`result_schema`** — a task-level rollup.
- **`Idempotency-Key`** — content-derived per logical batch, so a create that
  times out is retried with the *same* key and CALL-E returns the original call
  rather than dialling anyone twice.
- **`metadata`** — workflow, exception and attempt ids, echoed back on webhooks.
- **`webhook_url`** — terminal events, deduplicated on the event id.
- **`task_completed` + `completion_confidence`** — used as a *dampener*: low
  confidence can withhold an automatic resolution, but high confidence can never
  rescue evidence that is missing or ambiguous.
- **`recipients[].attempts[].transcript_turns`** — the conversation itself, shown
  in the detail view as a record. It is deliberately *not* an input to policy: a
  test asserts the decision engine never reads it, so no wording in a transcript
  can move business state.
- **`GET /v1/calls/{id}/events`** — CALL-E's own developer events are folded
  into the same audit timeline as our state transitions, so an operator can ask
  "the agent says the supplier confirmed — what did CALL-E actually see?"
- **`failure_code`** — stored and displayed, never branched on, because the API
  documents it as having no published enum.

[`docs/provider-truth.md`](docs/provider-truth.md) records every provider fact
this design depends on, cited to the OpenAPI contract.

## Reliability

Every claim below is a test in
[`backend/tests/services/test_reliability.py`](backend/tests/services/test_reliability.py),
run against real PostgreSQL.

| Failure | What happens |
|---|---|
| The same exception is triggered twice | One call. Eligibility refuses the second. |
| Create times out after CALL-E received it | Retried with the same key → one call **locally guaranteed**; provider-side dedup on a lost response is CALL-E's documented behaviour, not live-exercised (`docs/reliability-and-failure.md` §1b). |
| A terminal webhook is delivered twice | Second is a no-op; no second transition. |
| A webhook never arrives | The reconciliation sweep finishes the call. |
| CALL-E returns `structured_result: null` | Reconcile, then retry, then escalate. Never resolved. |
| An operator resolves it mid-call | The late result is kept as evidence; the state is not overwritten. |
| Two writers race | Optimistic version check rejects the stale write. |
| The API and the worker scan at the same instant | Row locks (`FOR UPDATE SKIP LOCKED`) give them disjoint sets, so two batches can never claim one supplier. |
| A terminal call carries no slice for an attempt | The attempt is failed and the exception released for retry — never stranded, never invented. |
| Attempts run out | Escalates to a human instead of dialling again. |
| A supplier asks to renegotiate price | Escalates, distinctly coded `COMMERCIAL_CHANGE_REQUESTED` — not a generic `needs_human` flag. |
| Right number, wrong person answers | Escalates even on a clean "yes, on time" — an authorized number is not an authorized person. |
| A supplier asks not to be called again | Escalates **and** blocklists *this exception*, so a later retry on it cannot dial them back. (Scoped per purchase order, not yet per phone number -- see `docs/provider-truth.md`.) |

## Safety

- The CALL-E key is backend-only and never enters the frontend bundle.
- Live calling needs **two** conditions — an explicit `CALLE_LIVE_CALLS=true`
  *and* a key — so no single stray environment variable starts dialling.
- `CALLE_ALLOWED_NUMBERS` gates live dispatch; a batch containing an
  unlisted number falls back to the simulator rather than calling.
- Operator-supplied text is scanned for payment cards (Luhn-checked), bank
  details, credentials, government identifiers and commercial-negotiation
  language before dispatch. A hit blocks the call and escalates — it never
  redacts and proceeds.
- Logs are structured JSON with API keys and phone numbers redacted.
- The matched value of a sensitive finding is never written to the audit trail;
  only the name of the rule that fired.

## What running it taught us

Three bugs in this list were found by running the real stack, not by
reasoning about it, and each is a test now:

- **Overlapping batches.** The API and the worker scanned for callable
  exceptions at the same instant and planned batches that shared
  suppliers. Two batches with different membership derive *different*
  idempotency keys, so the provider had no way to recognise them as the
  same work — against live CALL-E that is two real calls to one person.
  Fixed with row locks held from the eligibility check through the
  state transition.
- **Tests destroyed the demo.** The suite dropped every table in the
  same database the demo runs on. Tests now create and use their own.
- **A stale `alembic_version`.** `drop_all` leaves the version marker
  behind, so the next `upgrade head` is a no-op against an empty schema
  and the app starts with no tables. `init-db` now detects and repairs
  that.

## Known limitations

- **Two live calls have been placed; neither produced a real, populated
  answer.** The first (`call_uFfUVN5yyTTZFSmv2TnxSA`) rang and was not
  answered (fixture: `real_call_no_answer.json`). The second reached the
  recipient — `status: completed`, a 32-second call, a 5-turn transcript that
  our transcript mapper parsed correctly (fixture: `real_call_connected.json`)
  — but the recipient never spoke a substantive answer, so
  `structured_result` still came back all-`unknown`. Confirmed against real
  bytes across both: recipient ids are present and usable as the correlation
  anchor; the `po_status` rename avoided the reserved-name collision; both
  submitted schemas round-tripped; `completion_confidence` can be `0.82 high`
  on a call that did not complete (see `submission/calle-feedback.md`); and
  `transcript_turns` maps correctly end to end.

  What is still unproven: a call where the recipient actually answers the
  status questions, producing non-`unknown` `structured_result` values —
  and, since both fixtures predate schema v2, the new `spoke_with` /
  `escalation_reason` fields have **not** been exercised against real CALL-E
  bytes at all, only against the simulator and local tests. One more smoke
  test with the phone answered would close both.
- **No authentication.** Anyone who can reach the dashboard can trigger phone
  calls. Acceptable for a single-operator demo, not for a deployment.
- **No frontend tests.** ~900 lines of TypeScript rely on `tsc` and the backend
  contract tests. The decision was deliberate: the failure modes that matter
  here are in policy and provider handling, not in rendering.
- **Goals API not used.** `/v1/goals` publishes a stable error taxonomy that
  `/v1/calls` does not, and it is the better production target. Goals cannot be
  created through the API — they are authored in CALL-E Chat — so a Goal-based
  demo would not be reproducible from a clone. The adapter interface is shaped
  so a `GoalsCallProvider` can be added without touching policy.
- **Recipient correlation is by submission order**, because provider recipient
  ids first exist in the create response. A count mismatch is logged rather than
  silently truncated, and an attempt that cannot be correlated is released for
  retry rather than being stranded or given an invented outcome.
- **Ship-date parsing is deliberately conservative.** Ambiguous numeric forms
  like `09/11` are refused rather than guessed, because a two-month error on a
  delivery commitment is worse than an escalation.
- One vertical (supplier acknowledgement). The state machine and policy engine
  are workflow-agnostic; the schema and task text are not.
- `spoke_with` is a **self-reported** identity signal, not authentication. It
  proves someone claimed a role, not that the claim is true — appropriate here
  because the call discloses nothing before the claim is made and the outcome
  it gates is a routine status check, never a consequential action. See
  `docs/security-privacy.md`.
- **The demo deployment, if still running, predates this document's schema-v2
  work.** `docker compose ps` may show containers started in an earlier
  session; if so, rebuild (`docker compose up -d --build`) before relying on
  them to reflect the identity gate or caller-disclosure behaviour described
  above. Live calling stays disarmed either way unless `.env` says otherwise.

## The two CALL-E repositories

They do different jobs, and this project uses each for its own:

| Repository | Used here for |
|---|---|
| [call-e-integrations](https://github.com/CALLE-AI/call-e-integrations) | **Setup.** The official `calle-ai` SDK comes from here, and reading its `calls.create` source confirmed our request shape matches CALL-E's own client exactly — same endpoint, same field names, `Idempotency-Key` as a header. |
| [awesome-phone-call-agents](https://github.com/CALLE-AI/awesome-phone-call-agents) | **Submission.** The reusable pattern is packaged as a skill for this repository, and `scripts/check_skill.py` enforces its validation rules locally. |

## Submitting this project

The reusable pattern is packaged as a skill at
[`skills/exception-resolution-calls/`](skills/exception-resolution-calls/),
laid out for the
[awesome-phone-call-agents](https://github.com/CALLE-AI/awesome-phone-call-agents)
repository: `SKILL.md` with frontmatter, plus the required
`references/safety.md` and `references/examples.md`.

`python scripts/check_skill.py` mirrors that repository's validation
rules — kebab-case directory, frontmatter name matching the directory,
a description of at least 40 characters mentioning a phone or call
workflow, required reference files, local paths that exist and are
introduced with an actionable verb, no CJK characters, no trailing
whitespace, example-domain emails only — so a submission fails locally in
a second rather than in their CI after the PR is open.

README entry for that repository:

```markdown
- [exception-resolution-calls](skills/exception-resolution-calls/) - Resolve a blocked business workflow by phone, turning one CALL-E call task into per-recipient structured evidence and a deterministic state change.
```

## Checks

```bash
ruff check backend/        # lint
mypy backend/app           # types, clean across 45 files
pytest                     # 299 tests
bash scripts/check_secrets.sh   # no credential in the bundle, tree or logs
python scripts/check_skill.py   # submission passes the target repo's rules
```

CI runs all four on every push, plus an Alembic up/down/up round trip, against
real PostgreSQL — with no API key present, so it can never place a call.

## Documentation

| Doc | What is in it |
|---|---|
| [provider-truth.md](docs/provider-truth.md) | Every CALL-E fact this build depends on, cited to the OpenAPI spec, and the 10 places the original blueprint was wrong or incomplete |
| [architecture/high-level.md](docs/architecture/high-level.md) | System diagram and the webhook rule |
| [hackathon-build/spec.md](docs/hackathon-build/spec.md) | Technical specification |
| [prompts/decision-engine.md](docs/prompts/decision-engine.md) | The decision table, as pseudocode, including the identity gate and escalation differentiation |
| [reliability-and-failure.md](docs/reliability-and-failure.md) | Failure strategy, including the two-layer duplicate-call explanation |
| [security-privacy.md](docs/security-privacy.md) | Disclosure budget, data minimisation, and the weak-identity-factor design |
| [demo.md](docs/demo.md) | The three-minute demo, beat by beat, matching the actual current build |
| [verification-report.md](docs/verification-report.md) | What was checked, how, and the reported/verified-locally/verified-live distinction |
| [decision-log.md](docs/decision-log.md) | Why each documented conflict (A-F) was resolved the way it was |
| [submission/](submission/) | Draft Devpost copy, PR checklist, and sanitized CALL-E feedback — prepared, not published |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
