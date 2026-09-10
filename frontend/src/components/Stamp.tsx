import { labelOf, toneOf } from "../lib/api";

/** A status stamp, not a traffic light.
 *
 *  The three outcomes are peers: "needs a person" is a correct result of
 *  the agent running properly, so it carries no alarm colour. */
export function Stamp({ state }: { state: string }) {
  return <span className={`stamp ${toneOf(state)}`}>{labelOf(state)}</span>;
}
