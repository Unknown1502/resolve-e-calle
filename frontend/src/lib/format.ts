/** Presentation helpers shared across components. */

/** Clock time for the audit ledger. Dates are never shown — every entry
 *  in a chain of custody happened within minutes of the others. */
export function when(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Field order for the structured result, matching the order the agent
 *  asks the questions in. `spoke_with` and `escalation_reason` (schema
 *  v2) are last: they qualify the answer above them rather than being
 *  part of the core status question. Found missing here during a
 *  release-verification pass -- the backend always sent them, this list
 *  just never rendered them, so the one demo case that exists to prove
 *  the identity gate (PO-4827) showed its outcome in the decision panel
 *  but not in the raw evidence a person would want to check it against. */
export const EVIDENCE_ORDER = [
  "received",
  "po_status",
  "ship_date",
  "blocker",
  "needs_human",
  "spoke_with",
  "escalation_reason",
] as const;
