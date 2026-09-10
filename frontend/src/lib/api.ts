// The browser's entire view of the system. It talks only to the
// Resolve-E API -- the CALL-E key lives server-side and never ships here.

export type ExceptionSummary = {
  id: string;
  po_number: string;
  supplier_name: string;
  recipient_name: string | null;
  state: string;
  version: number;
  ack_due_at: string;
  attempt_count: number;
  hours_overdue: number;
  last_reason_text: string | null;
  is_live: boolean;
};

export type TranscriptTurn = {
  speaker: "bot" | "user" | "unknown";
  text: string;
  offset_seconds: number | null;
};

export type Attempt = {
  id: string;
  attempt_no: number;
  status: string;
  provider_recipient_id: string | null;
  recipient_status: string | null;
  structured_result: Record<string, string> | null;
  summary: string | null;
  transcript: TranscriptTurn[] | null;
  failure_code: string | null;
  failure_message: string | null;
  dispatched_at: string | null;
  completed_at: string | null;
};

export type Batch = {
  id: string;
  provider_call_id: string | null;
  status: string;
  is_live: boolean;
  idempotency_key: string;
  summary: string | null;
  task_completed: boolean | null;
  completion_confidence_score: number | null;
  completion_confidence_label: string | null;
  evidence: string[] | null;
  failure_code: string | null;
  failure_message: string | null;
  task_structured_result: Record<string, unknown> | null;
};

export type Decision = {
  decision: string;
  reason_code: string;
  reason_text: string;
  input_snapshot: Record<string, unknown>;
  created_at: string;
};

export type AuditEntry = {
  event_type: string;
  actor_type: string;
  previous_state: string | null;
  new_state: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export type ExceptionDetail = ExceptionSummary & {
  recipient_phone_masked: string;
  item_summary: string | null;
  attempts: Attempt[];
  batches: Batch[];
  decisions: Decision[];
  audit: AuditEntry[];
};

export type ResolveResult = {
  batch_id: string | null;
  provider_call_id: string | null;
  is_live: boolean;
  dispatched: number;
  refused: { exception_id: string; reason: string }[];
  message: string;
};

export type Health = {
  status: string;
  database: string;
  config: Record<string, unknown>;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => req<Health>("/health"),
  list: () => req<ExceptionSummary[]>("/api/exceptions"),
  detail: (id: string) => req<ExceptionDetail>(`/api/exceptions/${id}`),
  resolveAll: (ids: string[]) =>
    req<ResolveResult>("/api/exceptions/resolve", {
      method: "POST",
      body: JSON.stringify({ exception_ids: ids }),
    }),
};

// ── presentation vocabulary ──────────────────────────────────────
//
// Three PEER outcomes. "Needs a person" is a correct result, not a
// failure, so it never gets an alarm colour.

export type Tone = "resolved" | "attention" | "working" | "idle";

const TONES: Record<string, Tone> = {
  RESOLVED_ON_TIME: "resolved",
  RESOLVED_DELAYED: "resolved",
  HUMAN_REVIEW: "attention",
  CLOSED_UNRESOLVED: "attention",
  CALLING: "working",
  RECONCILING: "working",
  CALL_PLANNED: "working",
  RESULT_RECEIVED: "working",
  EVIDENCE_VALIDATED: "working",
  RETRY_PENDING: "working",
  ELIGIBLE: "idle",
  OPEN: "idle",
};

const LABELS: Record<string, string> = {
  OPEN: "Waiting on supplier",
  ELIGIBLE: "Ready to call",
  CALL_PLANNED: "Call queued",
  CALLING: "On the phone",
  RECONCILING: "Confirming with CALL-E",
  RESULT_RECEIVED: "Result in",
  EVIDENCE_VALIDATED: "Evidence checked",
  RESOLVED_ON_TIME: "Confirmed on time",
  RESOLVED_DELAYED: "Confirmed late",
  RETRY_PENDING: "Will try again",
  HUMAN_REVIEW: "Needs a person",
  CLOSED_UNRESOLVED: "Closed unresolved",
};

export const toneOf = (state: string): Tone => TONES[state] ?? "idle";
export const labelOf = (state: string): string => LABELS[state] ?? state;

export const FIELD_LABELS: Record<string, string> = {
  received: "Order received",
  po_status: "Fulfilment status",
  ship_date: "Ship date given",
  blocker: "Blocker named",
  needs_human: "Wants a person",
  spoke_with: "Who we reached",
  escalation_reason: "Why it needs a person",
};
