# Resolve-E PRD

## 1. Product summary

Resolve-E is a workflow automation product for operational exceptions. Its first workflow is supplier purchase-order acknowledgement.

## 2. User workflow

### Input

A purchase order contains:

- PO number
- supplier name
- supplier contact name
- authorized phone number
- order date
- expected acknowledgement deadline
- expected ship date
- items / quantity summary
- current status

### Trigger

A scheduled job or manual operator action identifies POs that are beyond the acknowledgement SLA.

### Agent action

Resolve-E:

1. checks that the PO is still unresolved;
2. checks that a call is permitted;
3. constructs a bounded call task;
4. creates the call through CALL-E;
5. receives or reconciles the terminal result;
6. validates the result against the schema;
7. applies deterministic policy;
8. persists the resulting state transition;
9. optionally creates the next action.

## 3. Call objective

The agent may ask:

- Have you received PO `<id>`?
- Can you confirm current order status?
- What is the expected ship date?
- Is there a blocker?
- Is a human follow-up required?

The agent may not:

- negotiate new commercial terms;
- provide payment credentials;
- approve price changes;
- commit the company to new contractual terms;
- disclose unnecessary internal information.

## 4. Internal outcome model

```text
confirmed_on_time
confirmed_delayed
blocked_need_human
recipient_unreachable
answer_unclear
call_failed
canceled_by_policy
```

## 5. State model

```text
OPEN
  ↓
ELIGIBLE
  ↓
CALL_PLANNED
  ↓
CALLING
  ↓
RESULT_RECEIVED
  ↓
EVIDENCE_VALIDATED
  ├── RESOLVED_ON_TIME
  ├── RESOLVED_DELAYED
  ├── RETRY_PENDING
  ├── HUMAN_REVIEW
  └── CLOSED_UNRESOLVED
```

## 6. UX requirements

The main dashboard must show:

- exception queue;
- severity;
- current workflow state;
- last action;
- next action;
- call count;
- resolution status.

The detail view must show:

- source PO;
- policy checks;
- CALL-E call ID;
- transcript/evidence summary where available;
- structured result;
- policy decision;
- audit trail.

## 7. Reliability requirements

- every logical call attempt has a deterministic idempotency key;
- duplicate terminal events are harmless;
- webhook processing is idempotent;
- polling/reconciliation can repair missed webhooks;
- no state transition skips validation;
- all outbound calls are bounded by policy;
- maximum attempts are enforced;
- terminal provider failure never becomes “resolved.”

## 8. Definition of done

A demo environment is complete only when the following scenarios pass:

1. supplier confirms receipt and on-time date;
2. supplier confirms delay;
3. supplier cannot be reached;
4. supplier gives an ambiguous answer;
5. CALL-E call creation is retried;
6. duplicate webhook is delivered;
7. structured result is null;
8. provider call fails;
9. workflow is already resolved when a delayed event arrives;
10. policy forbids a call;
11. maximum attempt threshold is reached;
12. human-review escalation is generated.
