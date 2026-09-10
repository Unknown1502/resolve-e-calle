import type { TranscriptTurn } from "../lib/api";

function stamp(offset: number | null): string {
  if (offset === null || offset === undefined) return "";
  const m = Math.floor(offset / 60);
  const s = offset % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * The conversation the evidence came from.
 *
 * Shown because "why did the agent decide that?" is not really
 * answerable without it. It is displayed as a record, never as an input:
 * the decision engine reads the structured result, so nothing a caller
 * says here can move a workflow on its own.
 */
export function Transcript({ turns }: { turns: TranscriptTurn[] }) {
  if (!turns.length) return null;
  return (
    <ol className="transcript-log">
      {turns.map((t, i) => (
        <li key={i} className={`turn ${t.speaker}`}>
          <span className="who">{t.speaker === "bot" ? "Resolve-E" : "Supplier"}</span>
          <p>{t.text}</p>
          <span className="at">{stamp(t.offset_seconds)}</span>
        </li>
      ))}
    </ol>
  );
}
