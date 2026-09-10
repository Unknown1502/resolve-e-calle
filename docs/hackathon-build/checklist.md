# Resolve-E Build Checklist

## Build mode

Recommended mode: autonomous implementation with verification after each high-risk integration step.

## 1. Scaffold backend + frontend
Spec ref: `spec.md > Architecture boundary`
What to build: Create the FastAPI backend, React frontend, configuration layer, and health endpoint.
Acceptance: App starts locally; frontend reaches backend health endpoint.
Verify: `backend` health check returns 200; frontend loads.

## 2. Create database schema
Spec ref: `spec.md > Optimistic concurrency`
What to build: Implement exceptions, workflow_runs, call_attempts, webhook_events, policy_decisions, audit_events, and outbox tables.
Acceptance: Migrations apply cleanly and constraints are present.
Verify: Migration up/down and schema inspection.

## 3. Implement domain state machine
Spec ref: `spec.md > Deterministic decision table`
What to build: Typed enums and transition guards.
Acceptance: Valid transitions pass; invalid transitions fail; terminal states are immutable.
Verify: `pytest tests/domain -q`.

## 4. Implement policy engine
Spec ref: `spec.md > Deterministic decision table`
What to build: Call eligibility, attempt budget, evidence policy, transition policy.
Acceptance: All mandatory policy cases produce expected deterministic decisions.
Verify: `pytest tests/policies -q`.

## 5. Implement CALL-E adapter
Spec ref: `spec.md > Provider adapter`
What to build: Backend-only CALL-E client with create/get/events methods.
Acceptance: Adapter returns internal normalized types and never leaks provider objects outside the adapter.
Verify: Mock provider tests + one authorized smoke call.

## 6. Implement idempotent call orchestration
Spec ref: `spec.md > Idempotency key`
What to build: Create attempts using deterministic idempotency keys and persist provider call ID safely.
Acceptance: Retrying the same logical attempt does not create a duplicate call.
Verify: Inject timeout and rerun creation with same key.

## 7. Implement webhook receiver + reconciliation
Spec ref: `spec.md > Reconciliation`
What to build: Lightweight webhook endpoint, dedupe, event persistence, queueing, and authoritative call reconciliation.
Acceptance: Duplicate events are harmless; missed webhooks are repairable.
Verify: Replay webhook twice and run reconciliation manually.

## 8. Implement structured-result validation
Spec ref: `spec.md > Structured result schema`
What to build: Schema validation, null-result handling, evidence normalization.
Acceptance: Invalid or ambiguous evidence cannot resolve a workflow.
Verify: Run all transcript fixtures.

## 9. Build Resolve-E dashboard
Spec ref: `prd.md > UX requirements`
What to build: Exception list, detail view, call evidence, policy decision, audit trail.
Acceptance: A judge can follow one exception from detection through resolution.
Verify: Manual walkthrough.

## 10. Add reliability and security checks
Spec ref: `reliability-and-failure.md`
What to build: retry budget, concurrency guards, redaction, API-key protection, call eligibility checks.
Acceptance: unsafe/duplicate/concurrent cases fail closed.
Verify: adversarial test suite.

## 11. Prepare demo scenario
Spec ref: `demo.md`
What to build: deterministic seeded PO dataset and one live authorized call scenario.
Acceptance: 3-minute story works from clean startup.
Verify: full demo run from empty database.

## 12. Devpost handoff
Spec ref: `demo.md`
What to build: README, architecture diagram, setup guide, demo video, screenshots, known limitations, and reusable skill/app documentation.
Acceptance: Project can be understood and evaluated without a verbal explanation.
Verify: fresh-machine setup checklist.
