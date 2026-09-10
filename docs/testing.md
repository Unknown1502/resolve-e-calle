# Resolve-E Testing Strategy

## Test pyramid

```text
                ┌───────────────┐
                │  E2E / Live   │
                │   CALL-E      │
                └───────┬───────┘
                ┌───────┴───────┐
                │ Provider      │
                │ integration   │
                └───────┬───────┘
        ┌───────────────┴────────────────┐
        │ policy + state machine tests   │
        └───────────────┬────────────────┘
              ┌─────────┴─────────┐
              │ unit / validation │
              └───────────────────┘
```

## Mandatory test cases

### Policy

- stale PO is eligible;
- fresh PO is rejected;
- max attempts prevents another call;
- restricted recipient blocks call;
- commercial negotiation triggers human review;
- ambiguous result triggers human review.

### State machine

- valid transitions succeed;
- invalid transitions are rejected;
- terminal state cannot be overwritten;
- optimistic concurrency conflict is handled.

### Idempotency

- same logical call attempt results in one provider call ID;
- retry after timeout reuses the same idempotency key.

### Webhooks

- first event is processed;
- duplicate event is a no-op;
- malformed event is rejected;
- delayed event can still be reconciled.

### Structured results

- valid result parses;
- unknown enum is preserved;
- missing required field is rejected;
- `structured_result = null` is handled safely;
- extra properties are rejected at the provider/schema layer and again at application validation.

### Provider outcomes

- completed;
- failed;
- canceled;
- in progress;
- temporary timeout.

### Conversation simulation

Use deterministic fake transcripts:

#### Scenario A — on-time

Supplier:
> “Yes, we received PO-4821. We will ship Friday.”

Expected:
```json
{
  "received": "yes",
  "status": "on_time"
}
```

#### Scenario B — delayed

Supplier:
> “We received it, but inventory is delayed. We can ship next Tuesday.”

Expected:
```json
{
  "received": "yes",
  "status": "delayed"
}
```

#### Scenario C — ambiguous

Supplier:
> “I believe someone in logistics has it.”

Expected:
```json
{
  "received": "unknown",
  "status": "unknown"
}
```

Expected final state: HUMAN_REVIEW.

#### Scenario D — commercial negotiation

Supplier:
> “We can ship tomorrow if you increase the price.”

Expected: HUMAN_REVIEW.

#### Scenario E — wrong person

Recipient:
> “I am not responsible for purchase orders.”

Expected: do not disclose unnecessary details; final outcome is retry/escalation according to policy.

## Live-call smoke test

Before the final demo:

1. call an authorized test number;
2. verify call ID;
3. verify terminal status;
4. verify structured result;
5. verify audit trail;
6. replay the terminal event;
7. verify no duplicate workflow transition.
