import { FIELD_LABELS } from "../lib/api";
import { EVIDENCE_ORDER } from "../lib/format";

/** Values worth calling out, across any evidence field: an unanswered
 *  question, or an identity claim that isn't the intended contact. Not
 *  just the literal string "unknown" -- `wrong_person` is just as much
 *  the reason an exception didn't resolve, and hiding that in plain
 *  black text would undercut the entire point of showing this field. */
const NEEDS_ATTENTION = new Set(["unknown", "wrong_person"]);

/** What the supplier said — a form filled in by voice. */
export function Evidence({ result }: { result: Record<string, string> }) {
  return (
    <div className="evidence">
      <dl>
        {EVIDENCE_ORDER.filter((k) => k in result).map((k) => (
          <div className="field" key={k}>
            <dt>{FIELD_LABELS[k] ?? k}</dt>
            <dd className={NEEDS_ATTENTION.has(result[k]) ? "unknown" : undefined}>
              {result[k]}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
