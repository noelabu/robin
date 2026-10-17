import type { Session } from "../api";
import { StateChip, STATE_LABEL } from "../map/Card";
import { sessionState } from "../map/useMap";

export const LABEL: Record<Session["runtime_state"], string> = { ...STATE_LABEL, unknown: "Unknown" };

/** State is shown as text, never colour alone (NFR-08); a guess says so, and a stale connection is said out loud. */
export function StateBadge({ session, now = Date.now() }: { session: Session; now?: number }) {
  const stale = session.connection_state !== "connected" && session.mode === "managed";
  const { state, inferred } = sessionState(session, now);
  return (
    <span className={`state state-${state}`}>
      <StateChip state={state} inferred={inferred} complete={false} />
      {inferred && <span className="state-note guess"> · guess</span>}
      {stale && <span className="state-note"> · {session.connection_state}</span>}
    </span>
  );
}
