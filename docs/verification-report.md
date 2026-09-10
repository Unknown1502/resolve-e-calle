# Resolve-E — Verification Report

Session scope: verify, harden, and package the existing implementation
for submission. No new architecture, no live calls placed this session,
no deployment or publication performed. Repository has never been
committed to git (`git status` shows 15 untracked top-level paths, zero
commits) -- that is a pre-existing condition, not something changed
here.

## Evidence classes used below

- **Reported** -- stated in a blueprint doc or a prior session's summary,
  not independently checked this session.
- **Verified locally** -- exercised this session against real
  PostgreSQL, real migrations, real HTTP (via `TestClient`), or the
  official `calle-ai` SDK's own source, but not against the live CALL-E
  network.
- **Verified via historical live fixture** -- backed by a real CALL-E
  response captured in an earlier session and committed as a fixture
  (`backend/tests/fixtures/*.json`). Replaying a fixture is **not** a
  new live call and is never described as one.
- **Verified live, this session** -- a real network call to CALL-E was
  placed during this session.
- **Unverified** -- neither implemented-and-tested nor exercised.

**This session placed zero live calls.** `CALLE_LIVE_CALLS` was found
armed (`true`) with an empty allowlist in `.env` at the start of this
session and was disarmed (`false`) before any verification work began,
and confirmed still disarmed at the end. Nothing in "verified via
historical live fixture" below happened this session; both fixtures
were captured previously and are replayed, not re-called.

## Findings table

| Requirement | Implementation evidence | Test evidence | Gap found | Priority | Status |
|---|---|---|---|---|---|
| Deterministic decision engine, no LLM authority | `app/policies/transition_policy.py::decide()` | `test_decision_engine.py`, `test_real_response.py` | none | -- | verified locally |
| Delayed resolution requires `received=yes` | `transition_policy.py` §10 (branch order) | `test_delay_without_receipt_is_inconsistent_and_escalates` | none -- already correct on inspection (Conflict A) | -- | verified locally |
| Provider field `po_status` not `status` | `adapters/calle/schemas.py`, `evidence_policy.py::WIRE_TO_INTERNAL` | `TestResultSchemaConstraints`, `test_real_response.py::test_the_po_status_rename_was_necessary_and_worked` | none -- already correct (Conflict B) | -- | **verified via historical live fixture** |
| Recipient identification represented as evidence | `spoke_with` field, schema v2; identity gate in `decide()` | `test_decision_engine.py` (new), `test_reliability.py::PO-4827` | **P0 -- was missing entirely** | P0 | fixed, verified locally |
| Differentiated escalation (refusal/dispute/commercial/suppression) | `escalation_reason` field, schema v2; branches 4-6 in `decide()` | new tests in `test_decision_engine.py`, suppression test in `test_reliability.py` | **P0 -- collapsed into one `needs_human` flag** | P0 | fixed, verified locally |
| Suppression on "do not call again" | `result_processor.py` blocklists the recipient | reliability suite | **P0 -- unrepresentable before this pass** | P0 | fixed, verified locally |
| Caller discloses AI status and represented company | `render_task()` opening + `Settings.buyer_company` / `disclosure_ready`; live dispatch refuses without it | `TestCallerDisclosure` (6 tests) | **P0 -- confirmed missing by a real transcript** | P0 | fixed, verified locally + **historical live fixture confirms the gap existed** |
| `region`/`locale` correct for the dialled number | `region_and_locale_for()`, longest-dial-code-prefix match | `TestRegionInference` | **P0 -- was hardcoded to `US`** (found and fixed in an earlier session, re-verified here) | P0 | verified locally + **historical live fixture confirms `IN` was accepted** |
| Row-locking prevents overlapping dispatch | `SELECT ... FOR UPDATE SKIP LOCKED`, `repo.lock_exception` | `test_concurrent_dispatch.py` races real Postgres sessions | none this session (fixed previously) | -- | verified locally |
| Idempotency key is content-derived, stable, batch-scoped | `commands.py::compute_batch_idempotency_key` | `TestIdempotencyKey` | none | -- | verified locally |
| Provider-side idempotency dedup on a lost response | CALL-E's documented header behaviour | none possible without inducing a dropped live response | **residual, provider-dependent, explicitly documented** (not a code gap) | -- | reported only; see `reliability-and-failure.md` §1b |
| Confidence dampens, never grants | `confidence_permits_resolution()`, one-directional | `TestCompletionConfidence`, **`TestHighConfidenceOnAFailedCall`** | none -- confirmed against real data | -- | **verified via historical live fixture** (0.82 "high" on a non-connected call) |
| Terminal-state protection (no late overwrite) | `state_machine.py`, `TerminalStateProtected` | `TestTerminalImmutability`, reliability suite | none | -- | verified locally |
| Webhook dedupe by event id | `repo.register_webhook_event`, UNIQUE constraint | `TestWebhookIdempotency`, API tests | none | -- | verified locally |
| Reconciliation repairs a lost webhook | `reconciliation_service.py` sweep | `TestReconciliationRepairsMissedWebhooks` | none | -- | verified locally |
| Official SDK is the live transport; HTTP client is a verified-identical fallback | `sdk_client.py`; `TestOfficialSdkProvider`, `test_our_request_shape_matches_the_sdk_exactly` | 9 tests | none | -- | verified locally (request shape); **verified via historical live fixture** (SDK actually used for both live calls, rate-limit error translated correctly) |
| Tests cannot inherit armed live calling | `conftest.py::_never_place_a_real_call` (session-scoped autouse) | asserts `live_calls_enabled is False` every run | **P0 found: `.env` was armed at session start; tests would have inherited it before this fixture existed in an earlier session** | P0 | fixed, verified locally |
| `.env.example` documents the real config variable | `BUYER_COMPANY` (not `RESOLVE_E_BUYER_COMPANY`) | `test_config.py` (new, 7 tests) | **self-introduced bug this session, caught before merge by writing the test first** | P0 | fixed, verified locally |
| Transcript never reaches the policy engine | source-inspection guard | `test_transcript_never_reaches_the_policy_engine` | none | -- | verified locally |
| Transcript stored/shown only in operator detail view, never in logs | `AttemptView` nested only under `ExceptionDetail`; no `log.*` call references `transcript` | inspection this session (grep, no matches) | none | -- | verified locally |
| Skill submission passes target-repo validation rules | `skills/exception-resolution-calls/`, `scripts/check_skill.py` | `OK: 1 skill(s) satisfy the submission rules.` | none | -- | verified locally against a local mirror of the documented rules (the live repo's own `validate_repository.py` was not re-fetched this session) |

## Commands run this session, with outcomes

```text
$ ruff check backend/                    -> All checks passed!
$ mypy backend/app                       -> Success: no issues found in 45 source files
$ pytest backend/tests -p no:warnings    -> 299 passed
$ python scripts/check_skill.py          -> OK: 1 skill(s) satisfy the submission rules.
$ bash scripts/check_secrets.sh          -> PASS (no credential in bundle/tree/logs)
$ grep CALLE_LIVE_CALLS .env             -> CALLE_LIVE_CALLS=false   (confirmed disarmed)
```

All commands run against the isolated `resolvee_test` PostgreSQL
database created by `tests/conftest.py`, never against the shared demo
database the (stale, still-running) `docker compose` containers use.

## Deployment status: reported vs actual

`docker compose ps` shows `api`, `postgres`, `redis`, `worker`, and a
public `cloudflared` `tunnel` container **already running**, started in
a prior session (up 17-20 hours at time of writing). This was found,
not started by this session.

Checked directly:

```text
$ docker exec resolve-e-api-1 python -c "... get_settings().live_calls_enabled"
live_calls_enabled: False
calle_live_calls  : False
allowed_numbers   : []
```

Confirmed **not armed for live calling**. But also confirmed **stale**:
calling `.disclosure_ready` on that container's `Settings` raises
`AttributeError` -- the running image predates this session's
`buyer_company`/schema-v2/identity-gate work entirely. **The public
tunnel, if still open, is exposing an old build.** No image rebuild or
redeploy was performed this session (redeploying a container reachable
from the public internet is treated as "deploying to an external
service" under the operating rules and requires explicit approval).

**Action needed from the user:** decide whether to (a) rebuild and
redeploy (`docker compose up -d --build api worker`, still safe -- live
calling stays disarmed by default and `BUYER_COMPANY` is unset, so
`disclosure_ready` is false either way), (b) leave it stale, or
(c) tear down the public tunnel (`docker compose stop tunnel`) if it is
no longer needed. None of these were done without asking.

## Residual, explicitly-documented limitation

The duplicate-call guarantee has two layers, and only one is fully
verified (`docs/reliability-and-failure.md` §1a/§1b, `docs/provider-truth.md`
§9): local row-locking is proven by racing real database sessions;
CALL-E's server-side idempotency-key dedup for a genuinely lost response
is documented provider behaviour, accepted by two live calls without
being rejected, but never exercised under an actual dropped-response
race, because doing so would mean deliberately breaking a live network
call. This is stated as a limitation, not implied as solved.

## What changed this session (summary; see `docs/provider-truth.md` §11
## and `docs/decision-log.md` for full reasoning)

1. Schema `supplier-exception.v1` -> `v2`: added `spoke_with` and
   `escalation_reason` (identity + differentiated escalation).
2. Identity gate and distinct escalation branches added to
   `transition_policy.decide()`, with suppression wired to the
   recipient blocklist.
3. Caller disclosure corrected: the task now states AI status and a
   real configured company before discussing order details; live
   dispatch refuses rather than substitutes a placeholder.
4. `region_and_locale_for()` region/locale inference verified still
   correct (fixed in an earlier session; re-confirmed here against a
   live-call fixture).
5. Test hermeticity: session-scoped fixture makes it structurally
   impossible for the suite to inherit an armed `.env`.
6. Self-caught bug: `.env.example` briefly documented the wrong
   environment variable name (`RESOLVE_E_BUYER_COMPANY` vs the real
   `BUYER_COMPANY`) during this session's own edit; caught by writing
   `test_config.py` before considering the work done, not after.
7. Documentation reconciled across `docs/prompts/*.md` (preserved as
   original intent, with addendum notes -- not rewritten),
   `docs/provider-truth.md` (new §11), `docs/reliability-and-failure.md`
   (new §1a/§1b), `docs/security-privacy.md`, `docs/demo.md` (rewritten
   to match the actual 8-PO / schema-v2 seed), and the skill package
   (`SKILL.md`, `references/examples.md`, `references/decision-table.md`,
   `references/result-schema.json` regenerated from the live schema).
8. New seeded demo case `PO-4827` (right number, wrong person)
   demonstrates the identity gate end-to-end, verified against real
   PostgreSQL.

Test count: 262 (start of session) -> **299** (end of session), all
passing. `mypy`/`ruff` clean throughout.
