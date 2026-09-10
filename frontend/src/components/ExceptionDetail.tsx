import type { ExceptionDetail as Detail } from "../lib/api";
import { when } from "../lib/format";
import { CallMeta } from "./CallMeta";
import { Evidence } from "./Evidence";
import { Stamp } from "./Stamp";
import { Transcript } from "./Transcript";

/**
 * The chain of custody for one purchase order.
 *
 * Read top to bottom it answers the only question that matters about an
 * autonomous agent: what did it do, on what evidence, and by what rule.
 */
export function ExceptionDetail({ detail }: { detail: Detail }) {
  const attempt = detail.attempts.at(-1) ?? null;
  const batch = detail.batches.at(-1) ?? null;
  const decision = detail.decisions.at(-1) ?? null;
  const result = attempt?.structured_result ?? null;

  return (
    <div className="detail">
      <header className="detail-head">
        <p className="po-big">{detail.po_number}</p>
        <h2>{detail.supplier_name}</h2>
        <div className="verdict">
          <Stamp state={detail.state} />
          {decision && <span className="rationale">{decision.reason_text}</span>}
        </div>
      </header>

      {detail.state === "HUMAN_REVIEW" && (
        <div className="banner warn">
          Resolve-E stopped here on purpose. The evidence did not meet the bar to
          close this order automatically, so it is waiting for you rather than
          guessing.
        </div>
      )}

      {batch && (
        <section className="block">
          <h3>The call</h3>
          <p className="lede">
            One CALL-E call task covered every supplier in this batch. Each was
            asked only about their own order.
          </p>
          <CallMeta batch={batch} attempt={attempt} />
          {attempt?.summary && <p className="transcript">{attempt.summary}</p>}

          {batch.evidence && batch.evidence.length > 0 && (
            <>
              <p className="lede evidence-lede">
                CALL-E's own supporting evidence for the outcome it reported.
              </p>
              <ul className="quotes">
                {batch.evidence.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </>
          )}
          {attempt?.failure_message && (
            <p className="transcript">
              Call failed: {attempt.failure_message}
              {attempt.failure_code ? ` (${attempt.failure_code})` : ""}
            </p>
          )}
        </section>
      )}

      {attempt?.transcript && attempt.transcript.length > 0 && (
        <section className="block">
          <h3>The conversation</h3>
          <p className="lede">
            What was actually said. Shown as a record, never as an input: the
            decision below is made from the structured result, so nothing said
            here can move the workflow on its own.
          </p>
          <Transcript turns={attempt.transcript} />
        </section>
      )}

      {result && (
        <section className="block">
          <h3>What the supplier said</h3>
          <p className="lede">
            Extracted by CALL-E against a strict schema, then checked again here
            before any of it counted as evidence.
          </p>
          <Evidence result={result} />
        </section>
      )}

      {detail.decisions.length > 0 && (
        <section className="block">
          <h3>Why Resolve-E acted</h3>
          <p className="lede">
            Every decision is made by deterministic policy. No language model
            chooses a workflow state.
          </p>
          <div className="decisions">
            {detail.decisions.map((d, i) => (
              <div className="decision" key={i}>
                <span className="code">
                  {d.decision} · {d.reason_code}
                </span>
                <p>{d.reason_text}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {detail.audit.length > 0 && (
        <section className="block">
          <h3>Chain of custody</h3>
          <p className="lede">
            Append-only. Every state change records who caused it and why.
          </p>
          <div className="ledger">
            {detail.audit.map((a, i) => (
              <div className="entry" key={i}>
                <time>{when(a.created_at)}</time>
                <div>
                  <div className="what">
                    {a.event_type}
                    <span className={`actor ${a.actor_type}`}>{a.actor_type}</span>
                  </div>
                  {a.previous_state && a.new_state && (
                    <div className="shift">
                      {a.previous_state}
                      <span className="arrow">to</span>
                      {a.new_state}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
