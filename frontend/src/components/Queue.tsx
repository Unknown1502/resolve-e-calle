import type { ExceptionSummary, ResolveResult } from "../lib/api";
import { Stamp } from "./Stamp";

/**
 * The exception queue.
 *
 * A table rather than cards: operations staff scan rows, and the whole
 * point of the screen is comparing many orders at a glance.
 */
export function Queue({
  rows,
  overdue,
  needsPerson,
  selected,
  busy,
  notice,
  error,
  onSelect,
  onCallAll,
}: {
  rows: ExceptionSummary[];
  overdue: ExceptionSummary[];
  needsPerson: ExceptionSummary[];
  selected: string | null;
  busy: boolean;
  notice: ResolveResult | null;
  error: string | null;
  onSelect: (id: string) => void;
  onCallAll: () => void;
}) {
  return (
    <div className="queue">
      <div className="queue-head">
        <p className="tally">
          <span className="qty">{overdue.length}</span> overdue
          {needsPerson.length > 0 && (
            <>
              {" · "}
              <span className="qty">{needsPerson.length}</span> need you
            </>
          )}
        </p>
        <p className="tally-sub">
          {overdue.length > 0
            ? "The software already knows these are stuck. Somebody still has to pick up the phone."
            : "Nothing is waiting on a call right now."}
        </p>

        <button className="call-all" onClick={onCallAll} disabled={busy || !overdue.length}>
          {busy
            ? "Placing call…"
            : overdue.length
              ? `Call ${overdue.length} supplier${overdue.length > 1 ? "s" : ""}`
              : "Nothing to call"}
        </button>

        {notice && (
          <p className="dispatch-note">
            {notice.message}
            {notice.provider_call_id && (
              <>
                {" "}
                <code>{notice.provider_call_id}</code>
              </>
            )}
          </p>
        )}
        {error && <p className="dispatch-note">Could not reach the API: {error}</p>}
      </div>

      <div className="rows">
        {rows.map((r) => (
          <button
            key={r.id}
            className="row"
            aria-current={selected === r.id}
            onClick={() => onSelect(r.id)}
          >
            <span className="po">{r.po_number}</span>
            <span className="supplier">{r.supplier_name}</span>
            <Stamp state={r.state} />
            <span className="overdue" style={{ gridColumn: 1 }}>
              {r.hours_overdue > 0 ? `${r.hours_overdue}h late` : "not due"}
            </span>
            {r.last_reason_text && <span className="why">{r.last_reason_text}</span>}
          </button>
        ))}
      </div>
    </div>
  );
}
