# Resolve-E — 3-Minute Demo Script and Shot List

This replaces the original blueprint script (20 POs, a single delayed
outcome, the v1 five-field schema). The current build seeds **8**
purchase orders; one batch call covers **7** of them (the 8th is not
yet due, proving the eligibility gate refuses to call early), landing
in **4 distinct states** via **7 distinct, individually reasoned
outcomes** from schema `supplier-exception.v2` -- verified by running
the actual `decide()` function against the seeded scenarios, not
asserted:

| PO | State | Reason code |
|---|---|---|
| PO-4821 | `RESOLVED_ON_TIME` | `EVIDENCE_SUFFICIENT_ON_TIME` |
| PO-4822 | `RESOLVED_DELAYED` | `EVIDENCE_SUFFICIENT_DELAYED` |
| PO-4823 | `HUMAN_REVIEW` | `EVIDENCE_AMBIGUOUS` |
| PO-4824 | `HUMAN_REVIEW` | `COMMERCIAL_CHANGE_REQUESTED` |
| PO-4825 | `RETRY_PENDING` | `RECIPIENT_NOT_REACHED` |
| PO-4826 | `RETRY_PENDING` | `STRUCTURED_RESULT_MISSING` |
| PO-4827 | `HUMAN_REVIEW` | `WRONG_PERSON` |

Every line below matches what `docker compose up --build` actually
produces -- verify with `docs/verification-report.md` before recording.

**Live-call status honestly, up front:** the demo below runs on the
deterministic simulator (`CALLE_LIVE_CALLS=false`, the default). Two
real calls were placed to one authorized number during development
(`docs/provider-truth.md` §9, fixtures in `backend/tests/fixtures/`);
neither is re-narrated as new evidence here. If a fresh, approved
smoke test is recorded before submission, splice it in at the point
marked **[LIVE INSERT]** below -- do not claim a call is live unless it
is the one in the video.

## 0:00–0:20 — The problem

Show the dashboard with the seeded queue.

Say:

> "Software already knows these purchase orders are stuck -- it has the
> PO, the supplier, the missed deadline. What it can't do is the last
> step: phone the supplier and ask."

Show the queue (`GET /api/exceptions`, or the dashboard):

```text
PO-4821  Acme Components      26h overdue
PO-4822  Globex Industrial    20h overdue
PO-4823  Initech Supply       42h overdue
PO-4824  Umbrella Materials   19h overdue
PO-4825  Stark Fabrication    55h overdue
PO-4826  Wayne Metals         61h overdue
PO-4827  Helix Fasteners      33h overdue
PO-4830  Cyberdyne Parts      not due yet
```

## 0:20–0:35 — One click, one CALL-E call task

Click **Resolve** (or `POST /api/exceptions/resolve` with an empty
`exception_ids` list -- "everything currently eligible").

Show the response naming **one** `call_id` covering 7 recipients
(PO-4830 is excluded -- not yet due, and the eligibility gate says so
in its own audit entry rather than silently skipping it).

Say:

> "One CALL-E call task. `recipients[]` plus `recipient_result_schema`
> gives each supplier an independent, structured outcome from a single
> dial-out."

**[LIVE INSERT — optional, approval required]:** if a fresh smoke test
has been recorded for this submission, show the real dashboard entry
for `call_...` here, with the real phone ringing.

## 0:35–1:10 — Six outcomes, one call, no model in the loop

Open PO-4821 first -- the clean case.

```json
{
  "received": "yes",
  "po_status": "on_time",
  "ship_date": "2026-09-11",
  "spoke_with": "intended_contact",
  "escalation_reason": "none"
}
```

State: `RESOLVED_ON_TIME`.

Then PO-4822 (delayed but received):

```json
{
  "received": "yes",
  "po_status": "delayed",
  "ship_date": "tomorrow",
  "spoke_with": "authorized_representative",
  "escalation_reason": "none"
}
```

State: `RESOLVED_DELAYED`. Say:

> "Delayed only resolves when the supplier also confirmed receipt and
> the parsed date is inside the policy threshold. A delay claim on its
> own is not enough."

## 1:10–1:45 — The identity gate (the new part)

Open **PO-4827**. The structured result looks clean:

```json
{
  "received": "yes",
  "po_status": "on_time",
  "spoke_with": "wrong_person",
  "escalation_reason": "none"
}
```

But the state is `HUMAN_REVIEW`, reason `WRONG_PERSON`.

Say:

> "An authorized phone *number* is not an authorized *person*. This
> call reached the right desk, and got a clean 'yes, on time' -- but
> the person who answered said they were not the PO contact. That
> answer cannot close the exception, however confident it sounds."

Then open PO-4824 (commercial ask) and show `escalation_reason:
commercial_change` -- distinctly coded from PO-4823's `HUMAN_REVIEW`
(ambiguous, second-hand answer) and PO-4827's `wrong_person`. Say:

> "Every human-review case used to collapse into one flag. Now an
> operator sees *why* without opening the transcript."

## 1:45–2:05 — Confidence can only take away, never give

Open the batch summary and point at `completion_confidence`.

Say:

> "During development, a live call that never connected came back with
> completion_confidence 0.82, 'high' -- CALL-E was confident the task
> did *not* complete. Read as a positive signal, that number would have
> closed a purchase order nobody discussed. Confidence can only
> withhold a resolution here. It can never grant one."

(Evidence: `backend/tests/fixtures/real_call_no_answer.json`,
`docs/calle-feedback.md`.)

## 2:05–2:30 — Reliability, shown not claimed

Trigger the same resolution again. Show:

> "No duplicate call created" -- the batch's idempotency key is
> content-derived; the same logical batch returns the same key.

Replay a webhook delivery. Show:

> "Duplicate terminal event ignored."

Say plainly, matching `docs/reliability-and-failure.md` §1:

> "That guarantee has two layers. Locally, database row-locking stops
> two of our own processes from ever planning overlapping batches --
> that's tested by racing real Postgres sessions. The provider-side
> idempotency key covers a request that reached CALL-E but whose
> response got lost -- that part is CALL-E's documented behaviour, and
> we have not tried to induce it live."

## 2:30–2:55 — Why it matters

```text
Before: human -> call -> wait -> update ERP -> repeat
With Resolve-E: exception -> CALL-E -> evidence -> policy -> resolution
```

Closing line:

> "Resolve-E does not automate calling. It automates the resolution of
> workflows that get stuck at the phone -- and it is honest about the
> one time it wasn't sure, which is exactly the case that matters."

## Shot list / evidence to capture

- [ ] dashboard queue, 8 POs, one not-yet-due;
- [ ] one `POST /resolve` -> one `call_id` covering 7 recipients;
- [ ] PO-4821 structured result + `RESOLVED_ON_TIME`;
- [ ] PO-4822 structured result + `RESOLVED_DELAYED`;
- [ ] PO-4827 structured result (clean "yes") + `HUMAN_REVIEW` / `WRONG_PERSON`;
- [ ] PO-4824 `escalation_reason: commercial_change` next to PO-4823's ambiguous case;
- [ ] `completion_confidence: 0.82 high` on the non-connected fixture, called out explicitly as a hazard;
- [ ] duplicate-trigger -> no duplicate call;
- [ ] duplicate webhook -> ignored;
- [ ] audit trail for one exception, full chain visible;
- [ ] **[LIVE INSERT, if approved]** a real `call_id` and real transcript.

## Do not demo

- real customer data, real financial information, real confidential procurement documents;
- unrestricted arbitrary calls;
- a live call the viewer has not approved recording;
- claiming any call is "live" that was not placed during this recording.
