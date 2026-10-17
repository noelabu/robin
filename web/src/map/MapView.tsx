import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type MapCard, type Session } from "../api";
import type { FeedItem } from "../useWorkspace";
import { Canvas, type CanvasHandle } from "./Canvas";
import { Card } from "./Card";
import { layout, PANEL_GAP } from "./layout";
import { RuntimeIcon } from "./RuntimeIcon";
import { TaskPanel } from "./TaskPanel";
import { ago } from "./time";
import { liveState, useMap, useTick } from "./useMap";

const SIDEBAR_SESSIONS = 5;

/** A file path shows as its name; a command or pattern as written. */
const shortTarget = (t: string | null) => (t && !/\s/.test(t) && t.includes("/") ? t.split("/").pop() : (t ?? ""));

/** Every session at a glance: cards clustered by repository on a pan-and-zoom canvas (read-only). */
export function MapView(props: {
  ws: { ready: boolean; feed: FeedItem[]; sessions: Record<string, Session>; attentionVersion: number; streamState: string };
  tabs: ReactNode;
  onOpenSession: (sessionId: string) => void;
  onOpenAttention: () => void;
  onChanged?: () => void;
}) {
  const { ws } = props;
  const map = useMap(ws);
  const now = useTick();
  const canvas = useRef<CanvasHandle>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [panelH, setPanelH] = useState(0);
  const [watching, setWatching] = useState<boolean | null>(null);
  const [needs, setNeeds] = useState(0);

  useEffect(() => {
    void api.watch().then((w) => setWatching(w.enabled)).catch(() => setWatching(null));
  }, []);
  useEffect(() => {
    void api.attention().then((a) => setNeeds(a.items.length)).catch(() => undefined);
  }, [ws.attentionVersion]);

  const data = map.data;
  const cards = data?.cards ?? [];
  const lay = useMemo(
    () => layout(map.order, data?.clusters ?? [], cards, selected && panelH ? { [selected]: panelH } : {}),
    [map.order, data?.clusters, cards, selected, panelH],
  );
  const state = (c: MapCard) => liveState(c, ws.sessions[c.session_id], now);
  const mains = cards.filter((c) => !c.parent);
  const working = mains.filter((c) => state(c).state === "running");
  const workingAt = working.reduce<string | null>(
    (m, c) => (c.last_activity_at && (!m || c.last_activity_at > m) ? c.last_activity_at : m),
    null,
  );
  const latest = mains.reduce<string | null>((m, c) => (c.last_activity_at && (!m || c.last_activity_at > m) ? c.last_activity_at : m), null);
  const sel = selected ? cards.find((c) => c.id === selected) : undefined;
  const clusterPath = (key: string) => data?.clusters.find((c) => c.key === key)?.path ?? key;
  const nowCard = data?.now;

  const open = (id: string) => {
    if (selected === id) return close();
    setPanelH(0);
    setSelected(id);
    const r = lay.cards[id];
    if (r) canvas.current?.reveal(r);
  };
  function close() {
    const id = selected;
    setSelected(null);
    if (id)
      requestAnimationFrame(() =>
        [...document.querySelectorAll<HTMLElement>("[data-card]")].find((el) => el.dataset.card === id)?.focus({ preventScroll: true }),
      );
  }

  return (
    <div className="map">
      <header className="map-top">
        <div className="map-brand">
          <span className="map-mark" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#111" strokeWidth="2.4" strokeLinecap="round">
              <path d="M5 19 12 5l7 14M8.5 13h7" />
            </svg>
          </span>
          <div>
            <h1>Robin</h1>
            <small>
              {data ? `${data.clusters.length} folders · ${data.clusters.reduce((n, c) => n + c.sessions, 0)} sessions` : "Map"}
            </small>
          </div>
        </div>
        <div className="map-status">
          {ws.streamState !== "live" ? (
            <span className="map-pill warn" role="status">
              <span className="dot" />
              Updates {ws.streamState}
            </span>
          ) : working.length ? (
            <span className="map-pill live" role="status">
              <span className="dot" />
              Working · {ago(workingAt, now)} ago
            </span>
          ) : (
            <span className="map-pill" role="status">
              <span className="dot" />
              Idle{latest && ` · ${ago(latest, now)} ago`}
            </span>
          )}
          {watching !== null && (
            <span className={`map-pill ${watching ? "on" : ""}`} title={watching ? "Following native history" : "Run robin watch on to follow sessions live"}>
              <span className="dot" />
              {watching ? "watching" : "not watching"}
            </span>
          )}
          {needs > 0 && (
            <button className="map-pill need" onClick={props.onOpenAttention}>
              {needs} need{needs === 1 ? "s" : ""} you
            </button>
          )}
        </div>
        {props.tabs}
      </header>

      <nav className="map-side" aria-label="Folders">
        <h2>Folders</h2>
        <ul>
          {(data?.clusters ?? []).map((cl) => {
            const frame = lay.frames.find((f) => f.key === cl.key);
            const recent = mains
              .filter((c) => c.cluster === cl.key)
              .sort((a, b) => (b.last_activity_at ?? "").localeCompare(a.last_activity_at ?? ""))
              .slice(0, SIDEBAR_SESSIONS);
            return (
              <li key={cl.key}>
                <button
                  className={`map-folder ${cl.running ? "hot" : ""}`}
                  title={cl.path}
                  onClick={() => frame && canvas.current?.panTo(frame)}
                >
                  <span className="name">{cl.label}</span>
                  <span className="map-age">{ago(cl.last_activity_at, now)}</span>
                </button>
                {recent.map((c) => (
                  <button key={c.id} className={`map-sess ${state(c).state === "running" ? "hot" : ""}`} onClick={() => open(c.id)}>
                    <span className="name">{c.title || `${c.runtime} · ${c.session_id.slice(0, 8)}`}</span>
                    <span className="map-age">{ago(c.last_activity_at, now)}</span>
                  </button>
                ))}
              </li>
            );
          })}
        </ul>
        <div className="map-legend">
          <i />
          working now · time since last activity
        </div>
      </nav>

      <Canvas
        ref={canvas}
        layout={lay}
        onBackground={close}
        overlay={
          <>
            {nowCard && (
              <button className="map-now" onClick={() => open(nowCard.card_id)} aria-label={`Now: ${nowCard.runtime} ${nowCard.tool}`}>
                <span className="hd">
                  <RuntimeIcon runtime={nowCard.runtime} />
                  <b>{nowCard.runtime}</b>
                  <span className="ctx">
                    {ago(nowCard.turn_started_at ?? nowCard.action_at, now)} · in {clusterPath(nowCard.cluster).split("/").pop()}
                  </span>
                </span>
                <p>{nowCard.title ?? "Untitled session"}</p>
                <span className="act">
                  <span title={nowCard.target ?? undefined}>
                    {nowCard.tool} {shortTarget(nowCard.target)}
                  </span>
                  <span>{ago(nowCard.action_at, now)}</span>
                </span>
              </button>
            )}
            {data && data.pending > 0 && (
              <div className="map-banner" role="status">
                Reading history… {data.pending.toLocaleString()} events left
              </div>
            )}
            {map.error && !data && (
              <div className="map-empty">
                <div role="alert">
                  <b>Couldn't load the map</b>
                  {map.error}
                  <br />
                  <button onClick={() => void map.reload()}>Try again</button>
                </div>
              </div>
            )}
            {data && !cards.length && (
              <div className="map-empty">
                <div>
                  <b>No sessions yet</b>
                  Run <code>robin scan</code> to index your Claude, Codex and Pi sessions, and <code>robin watch on</code> to
                  follow them live.
                </div>
              </div>
            )}
            {!data && !map.error && (
              <div className="map-empty" aria-busy="true">
                <div>Loading…</div>
              </div>
            )}
            <div className="map-hints" aria-hidden="true">
              <span>
                <kbd>click</kbd>Show tasks below card
              </span>
              <span>
                <kbd>drag</kbd>Move canvas
              </span>
              <span>
                <kbd>⌘</kbd>Scroll to zoom
              </span>
            </div>
          </>
        }
      >
        {(visible) => (
          <>
            {lay.frames.filter(visible).map((f) => {
              const cl = data?.clusters.find((c) => c.key === f.key);
              return (
                <div key={f.key} className="map-frame" style={{ left: f.x, top: f.y, width: f.w, height: f.h }}>
                  <span>
                    <b>{cl?.path}</b> · {cl?.sessions} session{cl?.sessions === 1 ? "" : "s"}
                  </span>
                  {f.more && (
                    <button
                      className="map-more"
                      style={{ left: f.more.x - f.x, top: f.more.y - f.y, width: f.more.w, height: f.more.h }}
                      onClick={(e) => {
                        e.stopPropagation();
                        void map.expand(f.key);
                      }}
                    >
                      +{f.more.count} more
                    </button>
                  )}
                </div>
              );
            })}
            {cards.map((c) => {
              const r = lay.cards[c.id];
              if (!r || !visible(r)) return null;
              const s = state(c);
              return (
                <Card
                  key={c.id}
                  card={c}
                  rect={r}
                  state={s.state}
                  inferred={s.inferred}
                  now={now}
                  selected={selected === c.id}
                  onOpen={(card) => open(card.id)}
                  onFocus={(rect) => canvas.current?.reveal(rect)}
                />
              );
            })}
            {sel && lay.cards[sel.id] && (
              <div
                style={{
                  position: "absolute",
                  left: lay.cards[sel.id].x,
                  top: lay.cards[sel.id].y + lay.cards[sel.id].h + PANEL_GAP,
                  width: Math.max(lay.cards[sel.id].w, 272),
                }}
              >
                <TaskPanel
                  key={sel.id}
                  card={sel}
                  state={state(sel).state}
                  session={ws.sessions[sel.session_id]}
                  now={now}
                  onHeight={setPanelH}
                  onOpenSession={props.onOpenSession}
                  onChanged={() => {
                    void map.reload();
                    props.onChanged?.();
                  }}
                  onClose={close}
                />
              </div>
            )}
          </>
        )}
      </Canvas>
    </div>
  );
}
