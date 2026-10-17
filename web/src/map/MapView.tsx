import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type MapCard, type MapCluster, type Session } from "../api";
import { useApprovals } from "../components/Approval";
import { RecencySwitch, WIDER } from "../components/RecencySwitch";
import { inRecency, type Recency } from "../recency";
import type { FeedItem } from "../useWorkspace";
import { Canvas, type CanvasHandle } from "./Canvas";
import { Card } from "./Card";
import { layout, PANEL_GAP } from "./layout";
import { assignHues, hue } from "./hue";
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
  recency?: Recency;
  onRecency?: (r: Recency) => void;
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
  const approvals = useApprovals(ws.attentionVersion);

  useEffect(() => {
    void api.watch().then((w) => setWatching(w.enabled)).catch(() => setWatching(null));
  }, []);
  useEffect(() => {
    void api.attention().then((a) => setNeeds(a.items.length)).catch(() => undefined);
  }, [ws.attentionVersion]);

  const data = map.data;
  const recency = props.recency ?? "all";
  const state = (c: MapCard) => liveState(c, ws.sessions[c.session_id], now);
  const filterNow = useTick(30_000); // recency moves slowly; re-filtering every second would re-lay the canvas
  const allCards = useMemo(() => data?.cards ?? [], [data]);
  const { counts, mains, cards, clusters } = useMemo(() => {
    const allMains = allCards.filter((c) => !c.parent);
    // A session whose workflow, shell or subagent still runs counts as working, even between its own turns.
    const busy = new Set(
      allCards.filter((c) => c.parent && liveState(c, undefined, filterNow).state === "running").map((c) => c.session_id),
    );
    const recent = (c: MapCard, r: Recency) =>
      c.id === selected ||
      busy.has(c.session_id) ||
      inRecency(liveState(c, ws.sessions[c.session_id], filterNow).state, c.last_activity_at, filterNow, r);
    const counts = {
      active: allMains.filter((c) => recent(c, "active")).length,
      today: allMains.filter((c) => recent(c, "today")).length,
      all: (data?.clusters ?? []).reduce((n, c) => n + c.sessions, 0),
    };
    if (recency === "all") return { counts, mains: allMains, cards: allCards, clusters: data?.clusters ?? [] };
    // Whatever a session started shows with it; a narrowed view has no "+N more", which would page in older sessions.
    const mains = allMains.filter((c) => recent(c, recency));
    const kept = new Set(mains.map((c) => c.session_id));
    const clusters: MapCluster[] = (data?.clusters ?? []).flatMap((cl) => {
      const n = mains.filter((c) => c.cluster === cl.key).length;
      return n ? [{ ...cl, sessions: n, shown: n }] : [];
    });
    return { counts, mains, cards: allCards.filter((c) => kept.has(c.session_id)), clusters };
  }, [data, allCards, recency, filterNow, ws.sessions, selected]);
  const lay = useMemo(
    () => layout(map.order, clusters, cards, selected && panelH ? { [selected]: panelH } : {}),
    [map.order, clusters, cards, selected, panelH],
  );
  const working = mains.filter((c) => state(c).state === "running");
  // Working folders claim distinct hues first, then the rest in sidebar order.
  assignHues([...working.map((c) => c.cluster), ...clusters.map((c) => c.key)]);
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
          <div>
            <h1 className="brand" aria-label="Robin">
              ROB<i>I</i>N
            </h1>
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
        {props.onRecency && <RecencySwitch value={recency} counts={counts} onChange={props.onRecency} />}
        <h2>Folders</h2>
        <ul>
          {clusters.map((cl) => {
            const frame = lay.frames.find((f) => f.key === cl.key);
            const recent = mains
              .filter((c) => c.cluster === cl.key)
              .sort((a, b) => (b.last_activity_at ?? "").localeCompare(a.last_activity_at ?? ""))
              .slice(0, SIDEBAR_SESSIONS);
            return (
              <li key={cl.key} style={hue(cl.key)}>
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
            {data && !allCards.length && (
              <div className="map-empty">
                <div>
                  <b>No sessions yet</b>
                  Run <code>robin scan</code> to index your Claude, Codex and Pi sessions, and <code>robin watch on</code> to
                  follow them live.
                </div>
              </div>
            )}
            {data && cards.length === 0 && allCards.length > 0 && recency !== "all" && (
              <div className="map-empty">
                <div>
                  <b>Nothing {recency === "active" ? "active" : "from today"}</b>
                  {recency === "active" ? "No session is working or waiting on you." : "No session has worked in the last day."}
                  <br />
                  <button onClick={() => props.onRecency?.(WIDER[recency].to)}>{WIDER[recency].label}</button>
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
              const cl = clusters.find((c) => c.key === f.key);
              return (
                <div key={f.key} className="map-frame" style={{ ...hue(f.key), left: f.x, top: f.y, width: f.w, height: f.h }}>
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
                  permission={!c.parent && approvals.bySession.has(c.session_id)}
                  question={!c.parent && approvals.questions.has(c.session_id)}
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
                  approvals={sel.parent ? undefined : approvals.bySession.get(sel.session_id)}
                  questions={sel.parent ? undefined : approvals.questions.get(sel.session_id)}
                  onAnswered={() => {
                    approvals.reload();
                    props.onChanged?.();
                  }}
                />
              </div>
            )}
          </>
        )}
      </Canvas>
    </div>
  );
}
