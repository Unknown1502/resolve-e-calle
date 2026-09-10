# Resolve-E Build Notes

## Risk order

1. Validate CALL-E authentication and a single real call.
2. Validate structured result extraction.
3. Validate idempotent create retry.
4. Validate webhook/reconciliation.
5. Build deterministic state machine.
6. Build dashboard.
7. Add polish.

## Integration checkpoint

Before implementing the full workflow, prove:

```text
backend
  ↓
CALL-E create
  ↓
authorized test phone
  ↓
terminal result
  ↓
structured JSON
```

Only after that should the full state machine depend on the provider.

## Operational rule

Never debug provider integration and business-state logic at the same time.

Use a mock provider for domain tests and a real CALL-E smoke test for provider tests.

## Final quality gate

A release candidate is acceptable only if:

- duplicate-call test passes;
- missed-webhook reconciliation passes;
- ambiguous-result test escalates;
- structured-result-null test escalates/retries safely;
- concurrent update does not overwrite terminal state;
- max-attempt policy works;
- no secret appears in frontend bundle or logs.
