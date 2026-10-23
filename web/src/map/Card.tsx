import type { JobStatus, MapCard, MapState } from "../api";
import type { Rect } from "./layout";
import { hue } from "./hue";
import { ago } from "./time";

export const STATE_LABEL: Record<MapState, string> = {
  running: "Working",
  waiting_for_input: "Needs input",
  idle: "Idle",
  failed: "Failed",
  stopped: "Stopped",
};

const basename = (p: string) => p.split("/").pop() || p;
const PING_MS = 4000; // how long a new action's label floats above the card

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

/** How a job ended, when the runtime said; otherwise its state. */
function JobChip({ status, state, inferred }: { status: JobStatus | null | undefined; state: MapState; inferred: boolean }) {
  if (status === "completed")
    return (
      <span className="map-chip done" title="Reported by the runtime">
        Completed
        <span className="map-tick" aria-hidden="true">
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="#fff" strokeWidth="1.8">
            <path d="M2 5.2 4.2 7.4 8 3" />
          </svg>
        </span>
      </span>
    );
  if (status === "killed") return <span className="map-chip killed">Killed</span>;
  return <StateChip state={state} inferred={inferred} complete={false} />;
}

/** A workflow's agents by phase: "Find 2/3 · 1 failed". */
function Phases({ phases }: { phases: NonNullable<MapCard["phases"]> }) {
  if (!phases.length) return <span className="phases">No agents yet</span>;
  return (
    <span className="phases">
      {phases.map((p, i) => (
        <span key={p.name || i}>
          {i > 0 && " · "}
          <b>{p.name || "Agents"}</b> {p.done}/{p.total}
          {p.failed > 0 && <span className="bad"> · {p.failed} failed</span>}
        </span>
      ))}
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
  permission?: boolean; // a permission request is waiting on the user
  question?: boolean; // the agent asked the user something
  className?: string;
  compact?: boolean; // the phone Map: a workflow counts its agents instead of listing phases
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
  const kind = card.kind ?? (sub ? "subagent" : "session");
  const job = kind === "skill" || kind === "workflow" || kind === "shell";
  const mini = kind === "subagent" && !!card.phase; // a workflow's agent
  const title = card.title || (job ? (card.label ?? kind) : `${card.runtime} · ${card.session_id.slice(0, 8)}`);
  const ended = card.status ? card.ended_at : null;
  const meta = mini ? [card.phase, card.label].filter(Boolean).join(" · ") : sub ? (card.label ?? "subagent") : card.runtime;
  // Skills, workflows and shells that ended a while ago lose their cards; the session's panel still lists them.
  const finished = (card.shells_done ?? 0) + (card.groups_done ?? 0);
  return (
    <button
      className={`map-card ${sub ? "sub" : ""} ${job ? kind : ""} ${mini ? "mini" : ""} ${state} ${props.selected ? "open" : ""} ${props.permission ? "permission" : props.question ? "question" : ""} ${props.className ?? ""}`}
      style={{ ...hue(card.cluster), left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      aria-expanded={props.selected}
      data-card={card.id}
      onClick={(e) => {
        e.stopPropagation();
        props.onOpen(card);
      }}
      onFocus={() => props.onFocus(rect)}
    >
      {card.tool && card.action_at && now - Date.parse(card.action_at) < PING_MS && (
        <span key={card.action_at} className="map-ping" aria-hidden="true">
          › {card.tool} {card.target && !/\s/.test(card.target) ? basename(card.target) : ""}
        </span>
      )}
      <span className="meta">
        <span className="rt">
          {meta}
          {!mini && card.last_activity_at && ` · ${ago(ended ?? card.last_activity_at, now)} ago`}
          {card.skip_permissions && (
            <span className="skip" title="Permission prompts skipped for this session">
              {" · skip permissions"}
            </span>
          )}
        </span>
        {props.permission ? (
          <span className="map-chip permission">Needs permission</span>
        ) : props.question ? (
          <span className="map-chip question">Needs an answer</span>
        ) : job || sub ? (
          <JobChip status={card.status} state={state} inferred={inferred} />
        ) : (
          <StateChip state={state} inferred={inferred} complete={complete} />
        )}
      </span>
      <h3>{title}</h3>
      {kind === "skill" && (card.inner?.length || card.args) && (
        <span className="inner">{card.inner?.length ? `→ ${card.inner.join(" → ")}` : card.args}</span>
      )}
      {kind === "workflow" &&
        (props.compact ? (
          <span className="phases">{(card.phases ?? []).reduce((n, p) => n + p.total, 0) + card.subagents_hidden} agents ›</span>
        ) : (
          <Phases phases={card.phases ?? []} />
        ))}
      {kind === "shell" && (card.target || card.summary) && (
        <span className="cmd" title={card.target ?? undefined}>
          {card.status && card.summary ? card.summary : card.target}
        </span>
      )}
      {!job && !mini && (
        <span className="line">
          <span>{progress}</span>
          {card.last_file && (
            <span className="file">
              {basename(card.last_file)}
              {card.files > 1 && ` +${card.files - 1}`}
            </span>
          )}
        </span>
      )}
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
            {props.selected ? "Hide tasks ↑" : props.permission ? "Review request ↓" : props.question ? "See question ↓" : "Show tasks ↓"}
            {card.subagents_hidden > 0 && ` · ${card.subagents_hidden} more subagents`}
            {finished > 0 && ` · ${finished} finished`}
          </span>
        </>
      )}
      {job && card.subagents_hidden > 0 && <span className="foot">{card.subagents_hidden} more agents</span>}
    </button>
  );
}
