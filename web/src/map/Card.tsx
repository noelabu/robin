import type { MapCard, MapState } from "../api";
import type { Rect } from "./layout";
import { ago } from "./time";

export const STATE_LABEL: Record<MapState, string> = {
  running: "Working",
  waiting_for_input: "Needs input",
  idle: "Idle",
  failed: "Failed",
  stopped: "Stopped",
};

const basename = (p: string) => p.split("/").pop() || p;

/** State as text, with a mark; an inferred state says so (NFR-08). */
export function StateChip({ state, inferred, complete }: { state: MapState; inferred: boolean; complete: boolean }) {
  return (
    <span className={`map-chip ${state}`} title={inferred ? "Inferred from recent activity" : "Reported by the runtime"}>
      {STATE_LABEL[state]}
      {state === "running" && <span className="map-spin" aria-hidden="true" />}
      {state !== "running" && complete && (
        <span className="map-tick" aria-hidden="true">
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="#fff" strokeWidth="1.8">
            <path d="M2 5.2 4.2 7.4 8 3" />
          </svg>
        </span>
      )}
    </span>
  );
}

export function Card(props: {
  card: MapCard;
  rect: Rect;
  state: MapState;
  inferred: boolean;
  now: number;
  selected: boolean;
  onOpen: (card: MapCard) => void;
  onFocus: (rect: Rect) => void;
}) {
  const { card, rect, state, inferred, now } = props;
  const t = card.todos;
  const complete = !!t && t.done === t.total;
  const progress = t
    ? `${t.done} of ${t.total} tasks · ${complete ? "Complete" : "In progress"}`
    : !card.detail
      ? "No detail for this runtime"
      : card.parent
        ? (card.target ?? "subagent")
        : "No task list";
  const sub = !!card.parent;
  const title = card.title || `${card.runtime} · ${card.session_id.slice(0, 8)}`;
  return (
    <button
      className={`map-card ${sub ? "sub" : ""} ${state}`}
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      aria-pressed={props.selected}
      data-card={card.id}
      onClick={(e) => {
        e.stopPropagation();
        props.onOpen(card);
      }}
      onFocus={() => props.onFocus(rect)}
    >
      <span className="meta">
        <span className="rt">
          {sub ? (card.label ?? "subagent") : card.runtime}
          {card.last_activity_at && ` · ${ago(card.last_activity_at, now)} ago`}
        </span>
        <StateChip state={state} inferred={inferred} complete={complete} />
      </span>
      <h3>{title}</h3>
      <span className="line">
        <span>{progress}</span>
        {card.last_file && (
          <span className="file">
            {basename(card.last_file)}
            {card.files > 1 && ` +${card.files - 1}`}
          </span>
        )}
      </span>
      {!sub && (
        <>
          <span
            className={`map-bar ${t ? "" : "none"}`}
            role={t ? "img" : undefined}
            aria-label={t ? `${t.done} of ${t.total} tasks` : undefined}
          >
            <i style={t ? { width: `${(t.done / t.total) * 100}%` } : undefined} />
          </span>
          <span className="foot">
            Show detail ›{card.subagents_hidden > 0 && ` · ${card.subagents_hidden} more subagents`}
          </span>
        </>
      )}
    </button>
  );
}
