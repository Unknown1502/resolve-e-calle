# Architecture and Workflow Diagrams

## 1. System context

```mermaid
flowchart LR
    O[Operations User] --> UI[Resolve-E Dashboard]
    UI --> API[Resolve-E API]
    API --> DB[(PostgreSQL)]
    API -->|writes| OUT[(Outbox Table)]
    OUT --> WRK[Worker]
    WRK -. advisory lock .-> R[(Redis)]
    WRK --> ORCH[Call Orchestrator]
    ORCH --> CE[CALL-E API]
    CE --> PHONE[Supplier Phone]
    CE --> WH[CALL-E Terminal Event]
    WH --> API
    API --> POLICY[Deterministic Policy Engine]
    POLICY --> DB
```

The outbox table -- not Redis -- is the queue: it is written in the same
transaction as the state change that produced it, which a Redis-backed queue
cannot be (`backend/app/workers/runner.py`). Redis is used only for an
advisory distributed lock and a wake-up nudge.

## 2. Closed-loop resolution

```mermaid
stateDiagram-v2
    [*] --> OPEN
    OPEN --> ELIGIBLE: exception detected
    ELIGIBLE --> CALL_PLANNED: policy permits
    ELIGIBLE --> HUMAN_REVIEW: policy blocks
    CALL_PLANNED --> CALLING: call accepted
    CALLING --> RESULT_RECEIVED: terminal result
    CALLING --> RECONCILING: webhook missing / uncertain
    RECONCILING --> RESULT_RECEIVED: terminal state confirmed
    RESULT_RECEIVED --> EVIDENCE_VALIDATED
    EVIDENCE_VALIDATED --> RESOLVED_ON_TIME: clear + compliant
    EVIDENCE_VALIDATED --> RESOLVED_DELAYED: delay accepted
    EVIDENCE_VALIDATED --> RETRY_PENDING: retryable failure
    EVIDENCE_VALIDATED --> HUMAN_REVIEW: ambiguous / consequential
    RETRY_PENDING --> CALL_PLANNED: budget available
    RETRY_PENDING --> HUMAN_REVIEW: budget exhausted
    HUMAN_REVIEW --> [*]
    RESOLVED_ON_TIME --> [*]
    RESOLVED_DELAYED --> [*]
```

## 3. Sequence diagram

```mermaid
sequenceDiagram
    participant UI as Dashboard
    participant API as Resolve-E API
    participant DB as PostgreSQL
    participant W as Worker
    participant CE as CALL-E
    participant S as Supplier
    participant P as Policy

    UI->>API: Start resolution(exception_id)
    API->>DB: Load + validate state
    API->>DB: Create workflow_run + attempt
    API->>W: Enqueue attempt
    W->>DB: Lock attempt
    W->>P: Check call eligibility
    W->>CE: Create call + idempotency key
    CE->>S: Phone call
    S-->>CE: Conversation
    CE-->>API: terminal webhook
    API->>DB: Deduplicate + enqueue reconciliation
    W->>CE: Get call
    CE-->>W: terminal call + structured result
    W->>P: Validate evidence
    P-->>W: Decision
    W->>DB: Atomic state transition
    W-->>UI: State + evidence available
```

## 4. Failure-aware call flow

```mermaid
flowchart TD
    A[Need outbound call] --> B{Policy allows?}
    B -->|No| H[Human Review]
    B -->|Yes| C[Generate deterministic idempotency key]
    C --> D[CALL-E create]
    D -->|Success| E[Persist call_id + CALLING]
    D -->|Timeout / 5xx| F{Safe retry?}
    F -->|Yes| D
    F -->|No| H
    E --> G[Webhook or polling]
    G --> I[Fetch authoritative call]
    I --> J{Terminal?}
    J -->|No| G
    J -->|Yes| K{Structured result valid?}
    K -->|No / null| L{Reconcile / retry budget?}
    L -->|Yes| G
    L -->|No| H
    K -->|Yes| M[Deterministic policy]
    M -->|Resolved| N[Close]
    M -->|Retry| D
    M -->|Escalate| H
```

## 5. Data lineage

```mermaid
flowchart LR
    PO[Purchase Order] --> EX[Exception]
    EX --> WR[Workflow Run]
    WR --> AT[Call Attempt]
    AT --> CE[CALL-E Call ID]
    CE --> EV[Terminal Evidence]
    EV --> SR[Structured Result]
    SR --> DEC[Policy Decision]
    DEC --> ST[State Transition]
    ST --> AU[Audit Event]
```
