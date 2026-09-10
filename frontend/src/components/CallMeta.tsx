import type { Attempt, Batch } from "../lib/api";

/** Provider facts for one call: the ids a judge can check, and the
 *  confidence score that governs whether a resolution was permitted. */
export function CallMeta({
  batch,
  attempt,
}: {
  batch: Batch;
  attempt: Attempt | null;
}) {
  const score = batch.completion_confidence_score;
  return (
    <div className="callmeta">
      <div>
        <div className="k">CALL-E call</div>
        <div className="v">{batch.provider_call_id ?? "not yet created"}</div>
      </div>
      {attempt?.provider_recipient_id && (
        <div>
          <div className="k">This supplier</div>
          <div className="v">{attempt.provider_recipient_id}</div>
        </div>
      )}
      <div>
        <div className="k">Idempotency key</div>
        <div className="v">{batch.idempotency_key}</div>
      </div>
      {score !== null && (
        <div>
          <div className="k">CALL-E confidence</div>
          <div className="v confidence">
            <span className="meter">
              <i style={{ width: `${Math.round(score * 100)}%` }} />
            </span>
            {score.toFixed(2)} {batch.completion_confidence_label}
          </div>
        </div>
      )}
      <div>
        <div className="k">Placed</div>
        <div className="v">{batch.is_live ? "real call" : "simulated"}</div>
      </div>
    </div>
  );
}
