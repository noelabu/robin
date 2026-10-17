import type { Recency } from "../recency";

const LABEL: Record<Recency, string> = { active: "Active", today: "Today", all: "All" };

/** Active / Today / All, each with how many sessions it shows, so nothing is hidden silently. */
export function RecencySwitch(props: { value: Recency; counts: Record<Recency, number>; onChange: (r: Recency) => void }) {
  return (
    <div className="recency" role="group" aria-label="Show sessions">
      {(["active", "today", "all"] as const).map((r) => (
        <button key={r} aria-pressed={props.value === r} onClick={() => props.onChange(r)}>
          {LABEL[r]} <span className="count">{props.counts[r].toLocaleString()}</span>
        </button>
      ))}
    </div>
  );
}

/** What to offer when a narrower view is empty: the next wider one. */
export const WIDER: Record<Exclude<Recency, "all">, { to: Recency; label: string }> = {
  active: { to: "today", label: "Show today's sessions" },
  today: { to: "all", label: "Show all sessions" },
};
