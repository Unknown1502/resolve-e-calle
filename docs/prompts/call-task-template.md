# CALL-E Task Prompt Template

Use a deterministic template to produce the task passed to CALL-E.

```text
Call the authorized supplier contact for purchase order {{po_number}}.

Goal:
Obtain factual status information needed to resolve the supplier acknowledgement exception.

You may disclose only:
- supplier name;
- purchase order number;
- brief item-category summary if necessary.

Ask, in this order:
1. Confirm you are speaking with the supplier contact or an authorized representative.
2. Confirm whether the purchase order was received/acknowledged.
3. Ask the current order status: on time, delayed, blocked, or unclear.
4. If delayed or blocked, ask for the expected ship date if one is known.
5. Ask for a short reason if the supplier volunteers one.
6. Ask whether they need a human follow-up.

Rules:
- Do not negotiate prices or terms.
- Do not request payment information or credentials.
- Do not invent answers.
- Use unknown when the evidence is insufficient.
- If the recipient requests a human, end politely and mark the outcome as requiring human review.
- If the wrong person answers, disclose no unnecessary order information.
- The objective is fact collection, not commitment.

Return structured data according to schema {{schema_version}}.
```

## Prompt construction rules

The application must generate this task from validated fields only.

Reject the task before creating a call when:

- phone number is not valid E.164;
- recipient is not authorized;
- exception is already resolved;
- attempt budget is exhausted;
- quiet hours / calling policy blocks the call;
- required context is missing;
- task text contains prohibited sensitive data;
- the call would create an unbounded objective.

## Example result schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "received",
    "status",
    "ship_date",
    "blocker",
    "needs_human"
  ],
  "properties": {
    "received": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "yes only when the supplier clearly confirms receipt"
    },
    "status": {
      "type": "string",
      "enum": ["on_time", "delayed", "blocked", "unknown"],
      "description": "Use unknown when evidence is insufficient"
    },
    "ship_date": {
      "type": "string",
      "description": "Supplier-stated expected ship date; use unknown when absent"
    },
    "blocker": {
      "type": "string",
      "enum": [
        "none",
        "inventory",
        "production",
        "transport",
        "administrative",
        "unknown"
      ],
      "description": "Only a blocker explicitly stated by the supplier"
    },
    "needs_human": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "yes when a human decision or follow-up is requested"
    }
  }
}
```

---

## Reconciliation note (implementation, not a rewrite of intent)

This file is preserved as the original design intent. The implemented
task template and schema (`backend/app/adapters/calle/schemas.py`)
differ in ways that satisfy this document's own stated rules more
completely than the original example did:

- `status` collides with a reserved CALL-E recipient field name and is
  sent as `po_status` on the wire (internal code still calls it
  `status`).
- Two fields were added -- `spoke_with` and `escalation_reason` --
  because "if the wrong person answers, disclose no unnecessary order
  information" and "mark the outcome as requiring human review" could
  not previously be represented as structured evidence, only as prose
  intent. See `docs/provider-truth.md` §11 for the full reasoning.
- The rendered task now opens by stating the agent is an AI assistant
  and naming a real, configured company, closing a gap a live call
  exposed directly (transcript in
  `backend/tests/fixtures/real_call_connected.json`).

The rules in this document (never invent answers, report unknown, no
negotiation, no credentials, no unnecessary disclosure to the wrong
person) are unchanged and are exactly what the implementation enforces.
