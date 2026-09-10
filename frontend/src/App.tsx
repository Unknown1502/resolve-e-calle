import { useCallback, useEffect, useMemo, useState } from "react";
import { ExceptionDetail } from "./components/ExceptionDetail";
import { Queue } from "./components/Queue";
import {
  api,
  type ExceptionDetail as Detail,
  type ExceptionSummary,
  type Health,
  type ResolveResult,
} from "./lib/api";

/** The worker resolves calls in the background, so the screen polls to
 *  stay honest about what has actually happened. */
const POLL_MS = 2000;

export default function App() {
  const [rows, setRows] = useState<ExceptionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<ResolveResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await api.list());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    void api.health().then(setHealth).catch(() => undefined);
    const t = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let live = true;
    const load = () =>
      api
        .detail(selected)
        .then((d) => live && setDetail(d))
        .catch(() => undefined);
    void load();
    const t = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [selected]);

  const overdue = useMemo(
    () =>
      rows.filter(
        (r) => ["OPEN", "RETRY_PENDING"].includes(r.state) && r.hours_overdue > 0,
      ),
    [rows],
  );
  const needsPerson = useMemo(
    () => rows.filter((r) => r.state === "HUMAN_REVIEW"),
    [rows],
  );

  const callAll = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      setNotice(await api.resolveAll(overdue.map((r) => r.id)));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [overdue, refresh]);

  const isLive = Boolean(health?.config?.live_calls_enabled);

  return (
    <div className="shell">
      <header className="masthead">
        <span className="wordmark">Resolve-E</span>
        <span className="context">supplier acknowledgements</span>
        <span className={`mode ${isLive ? "live" : ""}`}>
          <span className="bulb" />
          {isLive ? "Live calling on" : "Simulated calls"}
        </span>
      </header>

      <div className="panes">
        <Queue
          rows={rows}
          overdue={overdue}
          needsPerson={needsPerson}
          selected={selected}
          busy={busy}
          notice={notice}
          error={error}
          onSelect={setSelected}
          onCallAll={callAll}
        />

        {detail ? (
          <ExceptionDetail detail={detail} />
        ) : (
          <div className="detail">
            <div className="empty">
              <p>
                Pick a purchase order to see the whole chain: the call, what the
                supplier said, and why Resolve-E decided what it did.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
