# High-Level Architecture

## Goal

Create a reliable closed-loop system where CALL-E performs phone execution and Resolve-E owns workflow state.

```text
                         ┌─────────────────────┐
                         │     Operations UI    │
                         │ queue / detail /    │
                         │ evidence / audit    │
                         └──────────┬──────────┘
                                    │ HTTPS
                                    ▼
                         ┌─────────────────────┐
                         │    Resolve-E API    │
                         └──────────┬──────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                  │
                 ▼                  ▼                  ▼
        ┌────────────────┐ ┌────────────────┐ ┌─────────────────┐
        │ Workflow       │ │ Policy Engine  │ │ Audit Service   │
        │ State Machine  │ │ deterministic  │ │ append-only     │
        └───────┬────────┘ └────────────────┘ └────────┬────────┘
                │                                       │
                └────────────────┬──────────────────────┘
                                 ▼
                         ┌─────────────────┐
                         │   PostgreSQL    │
                         └─────────────────┘
                                 ▲
                                 │
                         ┌───────┴─────────┐
                         │ Worker / Queue  │
                         └───────┬─────────┘
                                 │
                                 ▼
                         ┌─────────────────┐
                         │ Call Orchestrator│
                         └───────┬─────────┘
                                 │
                                 ▼
                         ┌─────────────────┐
                         │  CALL-E Adapter │
                         └───────┬─────────┘
                                 │ HTTPS
                                 ▼
                         ┌─────────────────┐
                         │      CALL-E     │
                         └───────┬─────────┘
                                 │
                       phone conversation
                                 │
                                 ▼
                            Supplier
                                 │
                                 │ terminal event
                                 ▼
                         Webhook Receiver
                                 │
                                 ▼
                          Result Validator
                                 │
                                 ▼
                         Policy / State Loop
```

## Important architectural rule

The webhook is not itself the decision engine.

The webhook only reports:

> “A CALL-E task reached terminal state X.”

Resolve-E fetches/reconciles authoritative call state as needed, validates the structured result, and then runs deterministic business policy.
