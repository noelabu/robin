import { useEffect, useMemo, useState } from "react";
import { api, type Session } from "../api";
import type { FeedItem } from "../useWorkspace";

const EVIDENCE = ["reported", "observed", "inferred", "unknown"] as const;
const EVIDENCE_HELP: Record<(typeof EVIDENCE)[number], string> = {
  reported: "the runtime said so",
  observed: "Robin saw it happen",
  inferred: "Robin's conclusion from other events",
  unknown: "not classified",
};

export type FeedScope = { session_id?: string; task_id?: string; project_id?: string };

/** The scope's recent history plus live events. Agent text is plain text only (TR-070). */
export function ActivityFeed(props: {
  items: FeedItem[];
  scope: FeedScope;
  sessions: Record<string, Session>;
  ready: boolean;
}) {
  const [history, setHistory] = useState<FeedItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [family, setFamily] = useState("");
  const [evidence, setEvidence] = useState("");
  const key = JSON.stringify(props.scope);

  useEffect(() => {
    setLoaded(false);
    let live = true;
    void api
      .history(props.scope)
      .then((r) => live && setHistory([...r.events].reverse()))
      .catch(() => live && setHistory([]))
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const inScope = (i: FeedItem) => {
    const { session_id, task_id, project_id } = props.scope;
    if (session_id) return i.session_id === session_id;
    if (task_id) return i.task_id === task_id;
    if (project_id) return props.sessions[i.session_id]?.project_id === project_id;
    return true;
  };

  const all = useMemo(() => {
    const seen = new Set<string>();
    const merged: FeedItem[] = [];
    for (const i of [...props.items.filter(inScope), ...history]) {
      if (seen.has(i.event_id)) continue;
      seen.add(i.event_id);
      merged.push(i);
    }
    return merged.sort((a, b) => b.global_cursor - a.global_cursor);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.items, history, key]);

  const families = useMemo(() => [...new Set(all.map((i) => i.event_family))].sort(), [all]);
  const shown = all.filter((i) => (!family || i.event_family === family) && (!evidence || i.evidence_kind === evidence));
  const session = props.scope.session_id ? props.sessions[props.scope.session_id] : undefined;
  const gap = all.some((i) => i.event_type === "connection.gap");

  return (
    <section className="feed" aria-labelledby="feed-title">
      <div className="feed-head">
        <h2 id="feed-title">Activity</h2>
        <label className="inline">
          Event
          <select value={family} onChange={(e) => setFamily(e.target.value)}>
            <option value="">all</option>
            {families.map((f) => (
              <option key={f}>{f}</option>
            ))}
          </select>
        </label>
        <label className="inline">
          Evidence
          <select value={evidence} onChange={(e) => setEvidence(e.target.value)}>
            <option value="">all</option>
            {EVIDENCE.map((k) => (
              <option key={k} value={k}>
                {k} ({EVIDENCE_HELP[k]})
              </option>
            ))}
          </select>
        </label>
      </div>
      {gap && (
        <p className="hint warn" role="status">
          Coverage gap: some events in this view were not observed.
        </p>
      )}
      {!props.ready || !loaded ? (
        <p className="empty">Loading…</p>
      ) : !shown.length ? (
        <p className="empty">{emptyText(all.length > 0, session)}</p>
      ) : (
        <ol className="feed-list">
          {shown.map((i) => (
            <li key={i.event_id} className={`feed-item fam-${i.event_family}`}>
              <time dateTime={i.source_time ?? i.ingested_at}>
                {new Date(i.source_time ?? i.ingested_at).toLocaleTimeString()}
              </time>
              <span className="type">{i.event_type}</span>
              <span className={`evidence ev-${i.evidence_kind}`} title={EVIDENCE_HELP[i.evidence_kind]}>
                {i.evidence_kind}
              </span>
              {i.preview && <p className="preview">{i.preview}</p>}
              {i.payload_ref && <p className="preview muted">Large payload stored by reference</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** Say whether nothing happened or Robin couldn't see (PRD: empty states distinguish the two). */
function emptyText(filtered: boolean, session: Session | undefined): string {
  if (filtered) return "No events match these filters.";
  if (!session) return "No activity recorded here yet.";
  if (session.mode === "observed") return "No activity read from this session's transcript yet.";
  if (session.control_owner === "terminal")
    return "No activity recorded. The session is handed off to a terminal, so Robin may not see it.";
  if (session.connection_state !== "connected")
    return `No activity recorded, and Robin is ${session.connection_state} from this session, so activity may be missing.`;
  return "No activity yet. Robin is connected and will show events as they happen.";
}
