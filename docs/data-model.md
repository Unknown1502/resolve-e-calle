# Resolve-E Data Model

## exceptions

```text
id UUID PK
po_number TEXT UNIQUE NOT NULL
supplier_name TEXT NOT NULL
recipient_name TEXT
recipient_phone_e164 TEXT NOT NULL
ack_due_at TIMESTAMPTZ NOT NULL
expected_ship_date DATE
state TEXT NOT NULL
version INTEGER NOT NULL DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## workflow_runs

```text
id UUID PK
exception_id UUID FK
state TEXT NOT NULL
current_attempt_no INTEGER NOT NULL DEFAULT 0
version INTEGER NOT NULL DEFAULT 0
started_at TIMESTAMPTZ
completed_at TIMESTAMPTZ
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## call_attempts

```text
id UUID PK
workflow_run_id UUID FK
attempt_no INTEGER NOT NULL
provider TEXT NOT NULL
provider_call_id TEXT
idempotency_key TEXT UNIQUE NOT NULL
status TEXT NOT NULL
structured_result JSONB
summary TEXT
evidence JSONB
failure_code TEXT
failure_message TEXT
created_at TIMESTAMPTZ NOT NULL
completed_at TIMESTAMPTZ
UNIQUE(workflow_run_id, attempt_no)
```

## webhook_events

```text
id UUID PK
provider_event_id TEXT UNIQUE
provider_call_id TEXT
event_type TEXT
payload_hash TEXT
received_at TIMESTAMPTZ NOT NULL
processed_at TIMESTAMPTZ
```

## policy_decisions

```text
id UUID PK
workflow_run_id UUID FK
call_attempt_id UUID FK
decision TEXT NOT NULL
reason_code TEXT NOT NULL
reason_text TEXT NOT NULL
input_snapshot JSONB NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

## audit_events

```text
id UUID PK
entity_type TEXT NOT NULL
entity_id UUID NOT NULL
event_type TEXT NOT NULL
actor_type TEXT NOT NULL
payload JSONB NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

## outbox

```text
id UUID PK
dedupe_key TEXT UNIQUE NOT NULL
event_type TEXT NOT NULL
payload JSONB NOT NULL
status TEXT NOT NULL
attempts INTEGER NOT NULL DEFAULT 0
available_at TIMESTAMPTZ NOT NULL
delivered_at TIMESTAMPTZ
created_at TIMESTAMPTZ NOT NULL
```
