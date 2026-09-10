# Resolve-E Internal API Contract

## POST /api/exceptions

Create a seeded or external operational exception.

### Request

```json
{
  "po_number": "PO-4821",
  "supplier_name": "Acme Components",
  "recipient_name": "Jordan",
  "recipient_phone_e164": "+15550001111",
  "ack_due_at": "2026-09-09T12:00:00Z",
  "expected_ship_date": "2026-09-11"
}
```

### Response

```json
{
  "exception_id": "exc_123",
  "state": "OPEN"
}
```

## POST /api/exceptions/{id}/resolve

Starts resolution for one exception, if policy permits.

## POST /api/exceptions/resolve

Starts resolution for **several** exceptions in a single CALL-E call
task. An empty `exception_ids` list means "everything currently
eligible".

### Request

```json
{ "exception_ids": ["exc_123", "exc_124"] }
```

### Response

Both endpoints return the same shape.

```json
{
  "batch_id": "b7f1...",
  "provider_call_id": "call_abc123",
  "is_live": false,
  "dispatched": 2,
  "refused": [],
  "message": "Created 1 simulated call task covering 2 supplier(s)."
}
```

> **Deviation from the original design.** This response was specified as
> `{exception_id, workflow_run_id, state}` — one workflow run per call.
> Batch calling makes that shape wrong: a single CALL-E call task now
> covers *N* exceptions, each with its own workflow run, so the useful
> identifiers are the batch and the provider call. Per-exception state
> is read from `GET /api/exceptions/{id}`.
>
> `is_live` says whether a real phone call was placed. It is deliberately
> part of the contract so a simulated run can never be mistaken for a
> live one. See `docs/provider-truth.md` §3.

## GET /api/exceptions

Returns exception queue.

## GET /api/exceptions/{id}

Returns:

- source data;
- current state;
- attempts;
- provider call IDs;
- structured results;
- policy decisions;
- audit trail.

## POST /webhooks/calle

Receives terminal CALL-E events.

Rules:

1. validate shape;
2. dedupe by event ID when present;
3. store minimal event metadata;
4. enqueue reconciliation;
5. return success promptly.

## Provider adapter contract

```python
class CallProvider(Protocol):
    def create_call(self, command: CreateCallCommand) -> CallHandle: ...
    def get_call(self, call_id: str) -> NormalizedCall: ...
    def list_events(self, call_id: str) -> list[dict[str, Any]]: ...
```

Implemented by `CalleClient` (live) and `MockCalleProvider`
(deterministic). `list_events` is called during reconciliation and its
results are folded into the audit trail, so CALL-E's own view of the
call sits alongside our state transitions.

## Important provider behavior

The current CALL-E Calls API is asynchronous. It supports:

- `queued`
- `in_progress`
- `completed`
- `failed`
- `canceled`

The API can return structured results, task completion status, completion confidence, evidence, failure information, metadata, and timestamps. Call events can also be retrieved for reconciliation.


## Request correlation

Every response carries an `X-Request-ID` header, and every log line
emitted while handling that request carries the same id. An inbound
`X-Request-ID` is honoured (truncated to 128 printable characters) so a
proxy's trace id survives into our logs.

## GET /health

```json
{
  "status": "ok",
  "database": "ok",
  "config": { "live_calls_enabled": false, "calle_api_key_present": true, "...": "..." }
}
```

`config` reports whether a key is present. It never contains the key.
