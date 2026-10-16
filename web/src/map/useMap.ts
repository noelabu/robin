import { useCallback, useEffect, useRef, useState } from "react";
import { api, type MapCard, type MapCluster, type MapNow, type MapState, type Session } from "../api";
import type { FeedItem } from "../useWorkspace";
import { stableOrder } from "./layout";

const DEBOUNCE_MS = 750;
const RUNNING_WINDOW_MS = 60_000; // matches the server: inferred "working" lasts this long after activity
const LIVE_FAMILIES = new Set(["tool", "file", "message", "session"]);

type MapData = { clusters: MapCluster[]; cards: MapCard[]; now: MapNow | null; pending: number };

/** A card's state now: what the runtime reported, or else inferred from how recent its activity is. */
export function liveState(card: MapCard, session: Session | undefined, now: number): { state: MapState; inferred: boolean } {
  if (!card.parent && session && session.runtime_state !== "unknown") return { state: session.runtime_state, inferred: false };
  if (card.state_evidence === "reported") return { state: card.state, inferred: false };
  if (card.state === "stopped") return { state: "stopped", inferred: true };
  const last = card.last_activity_at ? Date.parse(card.last_activity_at) : 0;
  return { state: now - last < RUNNING_WINDOW_MS ? "running" : "idle", inferred: true };
}

/** The Map's data: a snapshot, then cards refreshed for the sessions the event stream says changed. */
export function useMap(ws: { ready: boolean; feed: FeedItem[] }) {
  const [data, setData] = useState<MapData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [order, setOrder] = useState<string[]>([]);
  const orderRef = useRef<string[]>([]);
  const dirty = useRef(new Set<string>());
  const lastCursor = useRef(0);

  const apply = useCallback((snap: Omit<MapData, "pending"> & { pending: number }) => {
    orderRef.current = stableOrder(orderRef.current, snap.clusters);
    setOrder(orderRef.current);
    setData(snap);
  }, []);

  const load = useCallback(async () => {
    try {
      const s = await api.map();
      apply({ clusters: s.clusters, cards: s.cards, now: s.now, pending: s.pending_events });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [apply]);

  const expand = useCallback(async (cluster: string) => {
    const s = await api.map(cluster);
    setData((d) =>
      d && {
        ...d,
        clusters: d.clusters.map((c) => s.clusters.find((x) => x.key === c.key) ?? c),
        cards: [...d.cards.filter((c) => c.cluster !== cluster), ...s.cards],
      },
    );
  }, []);

  useEffect(() => {
    if (ws.ready) void load();
  }, [ws.ready, load]);

  // While history is still being read, keep refreshing until it's caught up.
  useEffect(() => {
    if (!data?.pending) return;
    const t = window.setTimeout(() => void load(), 3000);
    return () => window.clearTimeout(t);
  }, [data, load]);

  // Sessions with new events since the last pass, refreshed together at most every DEBOUNCE_MS: a
  // timer already waiting isn't restarted, so a steady stream of events still refreshes.
  const dataRef = useRef(data);
  dataRef.current = data;
  const timer = useRef<number | null>(null);
  const flush = useCallback(async () => {
    timer.current = null;
    const ids = [...dirty.current].slice(0, 50);
    ids.forEach((id) => dirty.current.delete(id));
    if (!ids.length) return;
    try {
      const got = await api.mapCards(ids);
      const d = dataRef.current;
      if (d && got.cards.some((c) => !d.clusters.some((k) => k.key === c.cluster))) {
        void load(); // a new folder: the snapshot carries its cluster
        return;
      }
      const changed = new Set(ids);
      setData(
        (cur) =>
          cur && {
            ...cur,
            now: got.now,
            pending: got.pending_events,
            cards: [...cur.cards.filter((c) => !changed.has(c.session_id)), ...got.cards],
          },
      );
    } catch {
      ids.forEach((id) => dirty.current.add(id)); // try again with the next change
    }
    if (dirty.current.size && timer.current === null) timer.current = window.setTimeout(() => void flush(), DEBOUNCE_MS);
  }, [load]);

  useEffect(() => {
    for (const item of ws.feed) {
      if (item.global_cursor <= lastCursor.current) break; // the feed is newest first
      if (LIVE_FAMILIES.has(item.event_family)) dirty.current.add(item.session_id);
    }
    if (ws.feed.length) lastCursor.current = Math.max(lastCursor.current, ws.feed[0].global_cursor);
    if (dirty.current.size && timer.current === null) timer.current = window.setTimeout(() => void flush(), DEBOUNCE_MS);
  }, [ws.feed, flush]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return { data, order, error, reload: load, expand };
}

/** One clock for every "ago" label on the Map. */
export function useTick(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}
