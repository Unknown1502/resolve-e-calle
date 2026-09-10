# Master Coding-Agent Prompt — Resolve-E

You are the principal engineer implementing **Resolve-E** for the CALL-E hackathon.

Your job is to build the system exactly from the repository documentation. Do not invent a different architecture unless a documented provider constraint makes it necessary. When a provider detail is uncertain, isolate it behind the CALL-E adapter and verify against the official CALL-E docs before changing the rest of the application.

## PRODUCT

Resolve-E is an autonomous operational-exception resolution agent.

MVP:
Purchase order acknowledgement exception
→ policy validation
→ CALL-E phone call
→ structured evidence
→ deterministic decision
→ resolve / retry / human review.

Core product statement:

"When a business workflow gets stuck because a human needs to make a phone call, Resolve-E takes over."

## NON-NEGOTIABLE ARCHITECTURE

Use these layers:

1. Frontend
2. Resolve-E API
3. Domain/state machine
4. Deterministic policy engine
5. Call orchestrator
6. CALL-E provider adapter
7. PostgreSQL
8. Queue/worker
9. Webhook receiver
10. Reconciliation worker
11. Audit/outbox

The browser must never hold the CALL-E API key.

Only the provider adapter may contain CALL-E-specific implementation details.

## NON-NEGOTIABLE RELIABILITY RULES

1. Never let an LLM directly mutate workflow state.
2. Never interpret `structured_result = null` as success.
3. Never convert ambiguous evidence into a positive business decision.
4. Use a deterministic idempotency key per logical CALL-E call attempt.
5. Retry the same logical provider create operation with the SAME idempotency key.
6. Deduplicate terminal events.
7. Reconcile active calls from CALL-E when webhooks are missing or uncertain.
8. Never overwrite a terminal workflow state because of a late call result.
9. Use optimistic concurrency/version checks.
10. Persist audit evidence for every state transition.
11. Bound retries with a policy-controlled maximum.
12. Fail closed for commercial negotiation, payment-related actions, sensitive data requests, or other consequential actions.
13. Do not branch business logic on undocumented provider failure-code strings.
14. Do not expose unnecessary personal information to phone recipients.
15. Do not place arbitrary calls from uncontrolled user input.

## CALL-E INTEGRATION

Use the official CALL-E server SDK or REST API.

The integration must support at minimum:

- create call;
- retrieve call;
- retrieve call events;
- structured result schema;
- metadata containing workflow/exception/attempt identifiers;
- idempotency key;
- terminal status handling.

Normalize provider objects into internal domain objects.

## CALL TASK

The phone task must be bounded, explicit, and truthful.

It may:
- confirm intended contact;
- ask whether the PO was received;
- ask current status;
- ask expected ship date;
- ask a short blocker reason;
- ask whether human follow-up is required.

It must not:
- negotiate;
- approve price changes;
- collect payment credentials;
- ask for passwords;
- reveal unnecessary internal information;
- invent facts;
- make contractual commitments.

## STRUCTURED RESULT

Use a strict object schema with:

- received: yes/no/unknown
- status: on_time/delayed/blocked/unknown
- ship_date: string
- blocker: none/inventory/production/transport/administrative/unknown
- needs_human: yes/no/unknown

Use `additionalProperties: false`.

## DETERMINISTIC DECISION ENGINE

Implement decisions exactly as policy:

- received=yes + on_time → RESOLVED_ON_TIME
- received=yes + delayed + valid ship date within policy → RESOLVED_DELAYED
- needs_human=yes → HUMAN_REVIEW
- blocked → HUMAN_REVIEW
- unknown/ambiguous → HUMAN_REVIEW
- retryable failed call → RETRY_PENDING
- attempt budget exhausted → HUMAN_REVIEW
- call prohibited → no call

## UI REQUIREMENTS

Build a professional operations dashboard with:

- exception queue;
- status/severity;
- current state;
- call attempts;
- last action;
- next action;
- detail page;
- call evidence;
- structured result;
- policy decision;
- audit timeline.

The UI's main wow moment should be:

EXCEPTION DETECTED
→ POLICY CHECK
→ CALL-E CALL CREATED
→ SUPPLIER ANSWERS
→ STRUCTURED RESULT
→ POLICY DECISION
→ RESOLVED / HUMAN REVIEW

## IMPLEMENTATION PROCESS

Implement in this order:

1. scaffold;
2. database;
3. domain state machine;
4. policy engine;
5. CALL-E adapter;
6. idempotent orchestration;
7. webhook + reconciliation;
8. result validation;
9. UI;
10. reliability/security tests;
11. deterministic demo data;
12. final documentation.

## TEST-FIRST REQUIREMENT

Before declaring the project complete, create automated tests for:

- valid and invalid state transitions;
- policy decisions;
- duplicate call creation retry;
- duplicate webhook;
- missed webhook reconciliation;
- null structured result;
- malformed/unknown result;
- provider timeout;
- provider terminal failure;
- max attempts;
- concurrent operator update;
- human-review escalation;
- sensitive-data task rejection.

Also create at least one authorized live CALL-E smoke test.

## FAILURE HANDLING

Every external boundary must have:

- timeout;
- bounded retry;
- structured error mapping;
- logging;
- audit event;
- safe terminal behavior.

Do not silently swallow exceptions.

Do not catch `Exception` and continue with a successful state.

## OBSERVABILITY

Every workflow execution should carry:

- request ID;
- workflow run ID;
- exception ID;
- attempt number;
- CALL-E call ID.

Logs must be structured and redact sensitive values.

## DEFINITION OF DONE

The implementation is complete only when:

- clean startup works;
- database migrations work;
- health checks work;
- one real CALL-E test call succeeds;
- structured extraction succeeds;
- duplicate create retry is safe;
- duplicate webhook is harmless;
- reconciliation repairs missing webhook;
- ambiguous result escalates;
- commercial negotiation escalates;
- concurrent updates cannot overwrite terminal state;
- no secret reaches the frontend;
- complete demo works from a clean database.

## IMPORTANT

Do not optimize for "more AI".

Optimize for:
- deterministic behavior;
- provider correctness;
- observable state;
- safe failure;
- excellent demo experience.

The product wins because the agent **closes a real operational loop**, not because it merely talks on the phone.
