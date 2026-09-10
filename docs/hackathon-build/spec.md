# Resolve-E Technical Specification

## 1. Architecture boundary

The system contains five major subsystems:

```text
[Web UI]
    |
    v
[Resolve-E API]
    |
    +--> [Workflow State Machine] <--> [PostgreSQL]
    |
    +--> [Policy Engine]
    |
    +--> [Call Orchestrator] ---> [CALL-E Adapter] ---> [CALL-E]
    |
    +--> [Audit Service] -------> [PostgreSQL]
    |
    +<--- [CALL-E Webhook Receiver]
```

## 2. Provider adapter

Only `adapters/calle/` may know provider-specific details.

Interface:

```text
create_call(command) -> CallHandle
get_call(call_id) -> ProviderCall
list_events(call_id) -> ProviderEvents
```

The rest of the application works with internal types.

## 3. Call command

```json
{
  "workflow_run_id": "wr_123",
  "exception_id": "exc_123",
  "attempt_no": 1,
  "recipient_phone_e164": "+15550001111",
  "task": "bounded CALL-E task text",
  "result_schema_version": "supplier-exception.v1",
  "idempotency_key": "wr_123:attempt:1",
  "metadata": {
    "workflow_run_id": "wr_123",
    "exception_id": "exc_123",
    "attempt_no": 1,
    "schema_version": "supplier-exception.v1"
  }
}
```

## 4. Structured result schema

Use strict object schemas. Use enums with an explicit `unknown` value rather than coercing uncertainty into a boolean.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["received", "status", "ship_date", "blocker", "needs_human"],
  "properties": {
    "received": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "yes only if the supplier clearly confirms receipt."
    },
    "status": {
      "type": "string",
      "enum": ["on_time", "delayed", "blocked", "unknown"],
      "description": "Use unknown when the evidence is insufficient."
    },
    "ship_date": {
      "type": "string",
      "description": "Supplier-stated ship date; use unknown when not provided."
    },
    "blocker": {
      "type": "string",
      "enum": ["none", "inventory", "production", "transport", "administrative", "unknown"],
      "description": "Primary blocker explicitly stated by the supplier."
    },
    "needs_human": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "yes only when the supplier requests human follow-up or a consequential decision is required."
    }
  }
}
```

Do not rely on descriptions alone for correctness. Application-side policy must validate the semantics again.

## 5. Deterministic decision table

| Evidence | Decision |
|---|---|
| received=yes + status=on_time | resolve |
| received=yes + status=delayed + valid ship_date | resolve as delayed |
| received=no | retry or escalate according to policy |
| status=unknown | human review |
| needs_human=yes | human review |
| CALL-E failed | retry only if retry policy permits |
| structured_result=null | reconcile; otherwise retry/escalate |
| attempts >= max_attempts | human review / close unresolved |
| call prohibited by policy | do not call |

## 6. Optimistic concurrency

Every workflow update must use a version check:

```text
UPDATE exceptions
SET state = :new_state,
    version = version + 1
WHERE id = :id
  AND version = :expected_version;
```

If zero rows are updated, reload state and re-evaluate instead of forcing the transition.

## 7. Outbox pattern

When a workflow decision creates a side effect:

1. commit the state change and outbox record atomically;
2. worker sends the side effect;
3. mark outbox item delivered;
4. retry safely with idempotency.

This avoids the classic “database changed but call was never queued” split-brain bug.

## 8. Reconciliation

If webhook delivery is delayed or lost:

```text
CALLING
  ↓
reconciliation worker checks active call IDs
  ↓
GET call
  ↓
terminal?
  ├── no → remain CALLING
  └── yes → process terminal result
```

Use CALL-E call retrieval/events as the recovery path.

## 9. Security boundaries

- API key only in backend runtime.
- Phone numbers encrypted at rest where practical.
- Logs redact secrets and unnecessary personal data.
- Only authorized recipient numbers may be dialed.
- No unrestricted arbitrary phone targets from the UI.
- Human approval required for consequential commercial actions.
