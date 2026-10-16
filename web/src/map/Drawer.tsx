import { useEffect, useRef, useState } from "react";
import { api, type MapCard, type MapState, type SessionActivity } from "../api";
import { StateChip } from "./Card";
import { RuntimeIcon } from "./RuntimeIcon";
import { ago } from "./time";

/** One session's (or subagent's) todos and changed files, most recent first. */
export function Drawer(props: {
  card: MapCard;
  state: MapState;
  inferred: boolean;
  path: string;
  subagents: MapCard[];
  now: number;
  onOpenSession: (sessionId: string) => void;
  onClose: () => void;
}) {
  const { card, now } = props;
  const [detail, setDetail] = useState<SessionActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let live = true;
    api
      .sessionActivity(card.session_id)
      .then((d) => live && (setDetail(d), setError(null)))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [card.session_id, card.last_activity_at]);

  useEffect(() => heading.current?.focus({ preventScroll: true }), [card.id]);

  const laneId = card.parent ? card.id.slice(card.session_id.length + 1) : "";
  const lane = detail?.lanes.find((l) => l.lane === laneId);
  const title = card.title || `${card.runtime} · ${card.session_id.slice(0, 8)}`;
  const done = lane?.todos.filter((t) => t.status === "completed").length ?? 0;

  return (
    <aside
      className="map-drawer"
      aria-labelledby="map-drawer-title"
      onKeyDown={(e) => {
        if (e.key === "Escape") props.onClose();
      }}
    >
      <header>
        <div className="row">
          <RuntimeIcon runtime={card.runtime} />
          <b>{card.parent ? (card.label ?? "subagent") : card.runtime}</b>
          <StateChip state={props.state} inferred={props.inferred} complete={false} />
          <button className="x" aria-label="Close" onClick={props.onClose}>
            ×
          </button>
        </div>
        <h2 id="map-drawer-title" ref={heading} tabIndex={-1}>
          {title}
        </h2>
        <div className="path">
          {props.path}
          {card.last_activity_at && ` · last activity ${ago(card.last_activity_at, now)} ago`}
        </div>
      </header>
      <div className="body">
        {error && <p className="map-note">Couldn't load this session's activity: {error}</p>}
        {lane?.current && (
          <>
            <h3>Last action</h3>
            <div className="map-file hot">
              <span>
                {lane.current.tool} {lane.current.target ?? ""}
              </span>
              <span>{ago(lane.current.at, now)}</span>
            </div>
          </>
        )}
        <h3>
          <span>Tasks</span>
          <span>{lane && lane.todos.length ? `${done} / ${lane.todos.length}` : ""}</span>
        </h3>
        {!card.detail ? (
          <p className="map-note">Robin doesn't read task lists for {card.runtime} yet.</p>
        ) : lane?.todos.length ? (
          <ul>
            {lane.todos.map((t) => (
              <li key={t.id} className={`map-todo ${t.status}`}>
                <span className="box" aria-hidden="true" />
                <span>
                  {t.text}
                  <span className="sr-only"> ({t.status.replace("_", " ")})</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="map-note">{detail ? "No task list reported." : "Loading…"}</p>
        )}
        <h3>
          <span>Files changed</span>
          <span>{lane?.files_count || ""}</span>
        </h3>
        {!card.detail ? (
          <p className="map-note">Robin doesn't read file edits for {card.runtime} yet.</p>
        ) : lane?.files.length ? (
          <ul aria-label="Files changed">
            {lane.files.map((f, i) => (
              <li key={f.path} className={`map-file ${i === 0 && props.state === "running" ? "hot" : ""}`}>
                <span title={f.path}>{f.path}</span>
                <span>{ago(f.at, now)}</span>
              </li>
            ))}
            {lane.files_count > lane.files.length && (
              <li className="map-note">and {lane.files_count - lane.files.length} more</li>
            )}
          </ul>
        ) : (
          <p className="map-note">{detail ? "No file edits." : "Loading…"}</p>
        )}
        {props.subagents.length > 0 && (
          <>
            <h3>Subagents</h3>
            <ul>
              {props.subagents.map((s) => (
                <li key={s.id} className="map-file">
                  <span>{s.title || s.label || "subagent"}</span>
                  <span>{ago(s.last_activity_at, now)}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
      <footer>
        <button className="primary" onClick={() => props.onOpenSession(card.session_id)}>
          Open in Sessions
        </button>
        <button onClick={props.onClose}>Close</button>
      </footer>
    </aside>
  );
}
