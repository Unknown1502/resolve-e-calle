# Low-Level Architecture

## Backend modules

```text
backend/app/
├── api/
│   ├── exceptions.py
│   ├── calls.py
│   ├── webhooks.py
│   └── health.py
├── domain/
│   ├── models.py
│   ├── enums.py
│   └── commands.py
├── policies/
│   ├── call_eligibility.py
│   ├── attempt_policy.py
│   ├── evidence_policy.py
│   └── transition_policy.py
├── services/
│   ├── exception_service.py
│   ├── call_orchestrator.py
│   ├── result_validator.py
│   ├── reconciliation_service.py
│   └── audit_service.py
├── adapters/
│   └── calle/
│       ├── client.py
│       ├── mapper.py
│       └── schemas.py
├── workers/
│   ├── exception_scanner.py
│   ├── outbox_worker.py
│   └── reconciliation_worker.py
└── db/
    ├── models.py
    ├── repositories.py
    └── migrations/
```

## Request flow: create an exception

```text
POST /api/exceptions
    ↓
validate input
    ↓
repository.insert_exception()
    ↓
audit "exception.created"
    ↓
return exception_id
```

## Request flow: start resolution

```text
POST /api/exceptions/{id}/resolve
    ↓
load exception
    ↓
transition OPEN → ELIGIBLE
    ↓
policy.check_call_eligibility()
    ↓
create workflow_run
    ↓
enqueue call attempt
    ↓
worker executes attempt
```

## Worker flow: call creation

```text
load workflow_run
    ↓
lock row
    ↓
assert state == CALL_PLANNED
    ↓
policy.check_attempt_budget()
    ↓
build bounded task
    ↓
build strict result schema
    ↓
compute idempotency key
    ↓
CALL-E create call
    ↓
persist call_id
    ↓
state = CALLING
```

## Webhook flow

```text
POST /webhooks/calle
    ↓
validate payload shape
    ↓
dedupe using provider event ID
    ↓
store raw event metadata
    ↓
enqueue reconciliation
    ↓
HTTP 2xx
```

The webhook should stay lightweight. Do not run long business logic inside it.

## Reconciliation flow

```text
reconcile(call_id)
    ↓
GET CALL-E call
    ↓
is terminal?
  ├─ no → stop
  └─ yes
       ↓
persist terminal snapshot
       ↓
validate structured result
       ↓
policy.evaluate_result()
       ↓
compare workflow version
       ↓
apply transition
       ↓
append audit event
       ↓
enqueue next action if required
```

## Concurrency controls

- database row locks for stateful operations;
- optimistic version checks for state transitions;
- unique index on `(workflow_run_id, attempt_no)`;
- unique index on `(provider_event_id)`;
- unique outbox key per logical side effect.

## Idempotency key

```text
sha256(
  tenant_id +
  exception_id +
  workflow_run_id +
  attempt_no
)
```

The same logical attempt must reuse the same provider idempotency key.

## Error taxonomy

Classify errors into:

```text
VALIDATION
AUTHENTICATION
AUTHORIZATION
RATE_LIMIT
TRANSIENT_PROVIDER
PERMANENT_PROVIDER
CALL_OUTCOME
STRUCTURED_RESULT
POLICY
CONCURRENCY
INTERNAL
```

Retry only classes explicitly marked retryable.
