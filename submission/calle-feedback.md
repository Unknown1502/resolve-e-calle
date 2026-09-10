# CALL-E feedback (for the CALL-E Feedback Survey)

Two integration hazards found during development, both framed as
**hazards for other integrators to design around**, not as claims that
CALL-E is behaving incorrectly -- in both cases the behaviour is
internally consistent; it is just non-obvious from the field names
alone, and a developer building the "obvious" first version of the
integration would get it wrong.

---

## 1. `completion_confidence` can be high on a call that did not connect

**Reproduction context.** A live call was placed to an authorized
number via `POST /v1/calls` with a `recipient_result_schema`. The
recipient did not answer. The terminal response (call id
`call_uFfUVN5yyTTZFSmv2TnxSA`, retrieved via `GET /v1/calls/{id}`) was:

```json
{
  "status": "failed",
  "task_completed": false,
  "completion_confidence": { "score": 0.82, "label": "high" },
  "failure_code": "call_failed",
  "evidence": [
    "The call ended with no answer.",
    "No conversation or voicemail content was captured.",
    "The purchase-order status details were not collected."
  ],
  "structured_result": { "suppliers_reached": 0, "suppliers_confirmed": 0 },
  "recipients": [{
    "status": "failed",
    "structured_result": {
      "received": "unknown", "po_status": "unknown",
      "ship_date": "unknown", "blocker": "unknown", "needs_human": "unknown"
    }
  }]
}
```

(Full response committed at
`backend/tests/fixtures/real_call_no_answer.json`, with phone numbers
and account identifiers already absent from what the API returned for
this field set.)

**The hazard.** `completion_confidence.score = 0.82` reads, on first
encounter, as "CALL-E is 82% confident in a good outcome." It is
actually confidence in the **`task_completed` judgment**, which here is
`false` -- i.e., CALL-E is *highly confident the task did not
complete*. A developer who gates a business decision on
`completion_confidence.score >= threshold` alone, without first
checking `task_completed`, will treat this exact response as
high-confidence evidence and act on it.

**Developer impact.** In our system this would have meant: an automated
"confidence dampener" intended to withhold uncertain resolutions
instead granting one, because the score alone looked strong. We caught
it only because we built the dampener to check `task_completed is True`
*and* `completion_confidence.score >= threshold`, as two independent
conditions -- and wrote a test asserting a high score on a failed call
still blocks resolution. Any integration that checks only the score
field is exposed to this.

**Concrete suggestion.** Either (a) scope `completion_confidence` in the
docs explicitly as "confidence in `task_completed`, not confidence in
the outcome being favourable -- always check `task_completed` first,"
with this exact response shape as a worked example, or (b) consider
returning `completion_confidence: null` when `task_completed` is
`false`, so a score is only ever present alongside a positive judgment
it is actually scoring. We are not asserting which is correct API
design -- only that the current shape is easy to misread once, and hard
to notice you misread until it has already produced a wrong business
decision.

---

## 2. The free-plan daily call cap is not documented anywhere we found

**Reproduction context.** After two successful live calls in one
24-hour window, a third `POST /v1/calls` returned:

```text
HTTP 429
{ "error": { "code": "rate_limited" (or similar), "message": "The 24-hour call plan limit has been reached." } }
```

surfaced by both the official SDK (`CalleRateLimitError`) and our own
HTTP client identically.

**Developer impact.** None of the hackathon resources (`heycall-e.com`,
the API reference, `call-e-integrations`, or the Devpost resources page)
mention a per-day cap distinct from the "20 free calls" total, as far as
we found. A team budgeting calls across a multi-day build has no way to
know a day boundary exists at all until they hit it mid-session, which
is what happened here -- development paused waiting for the window to
roll over rather than for the total allowance to run out.

**Concrete suggestion.** Document the exact cap (calls per rolling
24-hour window, or per calendar day, and the reset boundary) alongside
the "20 free calls" figure on the hackathon resources page, and
consider having the 429 error `message` state the reset time explicitly
rather than "the limit has been reached" alone -- a developer hitting
this mid-build can plan around a known reset time but not around an
unknown one.

---

Both items are shared in the spirit of the "Most Valuable Feedback"
track: concrete, reproducible, with sanitized evidence, and framed as
integration guidance rather than a defect report, because neither
behaviour was shown to be incorrect -- only easy to misuse without
warning.
