# Resolve-E Product Vision

## Problem

Business workflows frequently stop at a human boundary:

- a supplier has not acknowledged an order;
- a vendor must confirm an ETA;
- a technician must accept an urgent job;
- a customer must confirm a change;
- a delivery exception needs a real person.

The problem is not that software cannot store the workflow. The problem is that the final coordination step still requires a phone conversation.

## Product thesis

**Phone calls should be an execution primitive inside an agentic workflow, not the entire product.**

Resolve-E turns a stalled workflow into a controlled resolution loop:

```text
Detect exception
      ↓
Validate policy
      ↓
Determine next action
      ↓
Call authorized party
      ↓
Capture structured evidence
      ↓
Validate evidence
      ↓
Advance / retry / escalate
      ↓
Resolved
```

## User

Primary user: operations / procurement staff at a small or mid-sized company.

The MVP does not require a real ERP. A realistic seeded dataset is enough to demonstrate the workflow.

## MVP user story

> As an operations manager, I want Resolve-E to identify stale purchase orders and call the correct supplier contact so that I do not manually chase every acknowledgement.

## MVP acceptance

For a seeded PO that is stale:

1. The dashboard shows the exception.
2. Resolve-E validates that the supplier is callable and the policy permits calling.
3. Resolve-E creates exactly one CALL-E attempt for that logical attempt.
4. The call asks only the approved questions.
5. The CALL-E result is mapped into the internal result schema.
6. The policy engine evaluates evidence.
7. If evidence is sufficient, the PO advances to the appropriate state.
8. If evidence is insufficient, the system does not invent an answer.
9. The UI shows the complete chain of evidence.
10. A second trigger/retry does not create a duplicate call.

## Non-goals for hackathon MVP

- full ERP integration;
- autonomous contract negotiation;
- payment collection;
- legal commitments;
- sensitive financial data exchange;
- unrestricted cold calling;
- open-ended phone conversations without a bounded task.

## Success metrics for the demo

- exception-to-resolution rate;
- median time to first contact;
- calls per resolved exception;
- duplicate-call count = 0;
- structured-result validation failure rate;
- percentage of ambiguous cases correctly escalated;
- policy violations blocked before a call.
