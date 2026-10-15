import { useCallback, useEffect, useReducer, useRef } from "react";
import { api, type Project, type RobinEvent, type Session, type Task, type Workspace } from "./api";

export type FeedItem = RobinEvent & { preview: string | null };

type State = {
  ready: boolean;
  sessions: Record<string, Session>;
  projects: Project[];
  tasks: Task[];
  observedTotal: number;
  feed: FeedItem[];
  seen: Set<string>;
  cursor: number;
  supervisorConnected: boolean;
  streamState: "connecting" | "live" | "reconnecting";
  gap: boolean;
  attentionVersion: number;
};

type Msg =
  | { kind: "snapshot"; ws: Workspace }
  | { kind: "older"; sessions: Session[] }
  | { kind: "event"; event: RobinEvent; session: Session; preview: string | null }
  | { kind: "stream"; state: State["streamState"] };

const FEED_LIMIT = 500; // bounded rendering; full history stays on the server

function reducer(s: State, m: Msg): State {
  switch (m.kind) {
    case "snapshot":
      return {
        ...s,
        ready: true,
        projects: m.ws.projects,
        tasks: m.ws.tasks,
        observedTotal: m.ws.observed_total,
        // keep older sessions already paged in; the snapshot carries the recent ones
        sessions: { ...s.sessions, ...Object.fromEntries(m.ws.sessions.map((x) => [x.session_id, x])) },
        cursor: Math.max(s.cursor, m.ws.watermark),
        supervisorConnected: m.ws.supervisor.connected,
      };
    case "event": {
      if (s.seen.has(m.event.event_id)) return s; // dedupe replays by event id (TR-034)
      const seen = new Set(s.seen);
      seen.add(m.event.event_id);
      const feed = [{ ...m.event, preview: m.preview }, ...s.feed].slice(0, FEED_LIMIT);
      return {
        ...s,
        seen,
        feed,
        cursor: Math.max(s.cursor, m.event.global_cursor),
        sessions: { ...s.sessions, [m.session.session_id]: m.session }, // upsert the projection
        gap: s.gap || m.event.event_type === "connection.gap",
        attentionVersion:
          ["attention", "session"].includes(m.event.event_family) || m.event.event_type.startsWith("control.")
            ? s.attentionVersion + 1
            : s.attentionVersion,
      };
    }
    case "stream":
      return { ...s, streamState: m.state };
    case "older":
      return {
        ...s,
        sessions: { ...Object.fromEntries(m.sessions.map((x) => [x.session_id, x])), ...s.sessions },
      };
  }
}

const initial: State = {
  ready: false,
  sessions: {},
  projects: [],
  tasks: [],
  observedTotal: 0,
  feed: [],
  seen: new Set(),
  cursor: 0,
  supervisorConnected: false,
  streamState: "connecting",
  gap: false,
  attentionVersion: 0,
};

/** Snapshot, then a stream from the snapshot's watermark; a resync request reloads the snapshot. */
export function useWorkspace(enabled: boolean) {
  const [state, dispatch] = useReducer(reducer, initial);
  const source = useRef<EventSource | null>(null);

  const connect = useCallback(async () => {
    source.current?.close();
    const ws = await api.workspace();
    dispatch({ kind: "snapshot", ws });
    const es = new EventSource(`/api/v1/stream?after=${ws.watermark}`); // reconnects send Last-Event-ID
    source.current = es;
    es.onopen = () => dispatch({ kind: "stream", state: "live" });
    es.onerror = () => dispatch({ kind: "stream", state: "reconnecting" });
    es.addEventListener("event", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      dispatch({ kind: "event", event: data.event, session: data.session, preview: data.preview });
    });
    es.addEventListener("resync_required", () => void connect());
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void connect();
    const refresh = window.setInterval(async () => {
      try {
        const ws = await api.workspace();
        dispatch({ kind: "snapshot", ws });
      } catch {
        /* the stream shows reconnecting */
      }
    }, 15000); // connection freshness and supervisor status between events
    return () => {
      window.clearInterval(refresh);
      source.current?.close();
    };
  }, [enabled, connect]);

  const reload = useCallback(async () => {
    dispatch({ kind: "snapshot", ws: await api.workspace() });
  }, []);

  const loadOlder = useCallback(async (offset: number) => {
    const page = await api.sessions(offset);
    dispatch({ kind: "older", sessions: page.sessions });
    return page.next_offset;
  }, []);

  return { ...state, reload, loadOlder };
}
