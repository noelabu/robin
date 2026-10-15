import type { Session } from "../api";

export const LABEL: Record<Session["runtime_state"], string> = {
  running: "Running",
  waiting_for_input: "Needs input",
  idle: "Idle",
  failed: "Failed",
  stopped: "Stopped",
  unknown: "Unknown",
};

/** State is shown as text, never colour alone (NFR-08). A stale connection is said out loud. */
export function StateBadge({ session }: { session: Session }) {
  const stale = session.connection_state !== "connected" && session.mode === "managed";
  return (
    <span className={`state state-${session.runtime_state}`}>
      {LABEL[session.runtime_state]}
      {stale && <span className="state-note"> · {session.connection_state}</span>}
    </span>
  );
}
