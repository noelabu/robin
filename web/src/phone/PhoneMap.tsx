import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MapCard, Session } from "../api";
import { useApprovals } from "../components/Approval";
import { WIDER } from "../components/RecencySwitch";
import type { Recency } from "../recency";
import type { FeedItem } from "../useWorkspace";
import { Card } from "../map/Card";
import { assignHues, hue } from "../map/hue";
import { TaskPanel } from "../map/TaskPanel";
import { liveState, narrow, useMap, useTick } from "../map/useMap";
import { folderNeeds, phoneLayout, pickFolder, type PhoneWire } from "./phoneLayout";
import { Sheet } from "./Sheet";

const HEAD: Record<NonNullable<PhoneWire["head"]>["dir"], (x: number, y: number) => string> = {
  down: (x, y) => `M${x - 4} ${y - 6} L${x} ${y} L${x + 4} ${y - 6}z`,
  right: (x, y) => `M${x - 6} ${y - 4} L${x} ${y} L${x - 6} ${y + 4}z`,
  left: (x, y) => `M${x + 6} ${y - 4} L${x} ${y} L${x + 6} ${y + 4}z`,
};

/** The Map on a phone: one folder at a time, its cards top to bottom. Tapping a card opens its panel in a sheet. */
export function PhoneMap(props: {
  ws: { ready: boolean; feed: FeedItem[]; sessions: Record<string, Session>; attentionVersion: number };
  recency: Recency;
  onRecency: (r: Recency) => void;
  onOpenSession: (sessionId: string) => void;
  onChanged?: () => void;
}) {
  const { ws, recency } = props;
  const map = useMap(ws);
  const now = useTick();
  const filterNow = useTick(30_000);
  const approvals = useApprovals(ws.attentionVersion);
  const [chosen, setChosen] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [sheet, setSheet] = useState<string | null>(null);
  const board = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(320);

  useLayoutEffect(() => {
    const measure = () => setWidth(board.current?.clientWidth || 320);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  const data = map.data;
  const { mains, cards, clusters } = useMemo(
    () => narrow(data, recency, ws.sessions, filterNow, sheet),
    [data, recency, ws.sessions, filterNow, sheet],
  );
  const state = (c: MapCard) => liveState(c, ws.sessions[c.session_id], now);
  const needs = (c: MapCard) =>
    !c.parent &&
    (approvals.bySession.has(c.session_id) || approvals.questions.has(c.session_id) || state(c).state === "waiting_for_input");
  const needCounts = folderNeeds(mains, needs);
  const ordered = map.order.flatMap((k) => clusters.filter((c) => c.key === k));
  assignHues(ordered.map((c) => c.key));
  const folder = pickFolder(ordered, needCounts, chosen);
  const shown = cards.filter((c) => c.cluster === folder);
  const lay = phoneLayout(shown, { width, open, needs });
  const sel = sheet ? cards.find((c) => c.id === sheet) : undefined;
  const toggle = (id: string) =>
    setOpen((o) => {
      const n = new Set(o);
      if (!n.delete(id)) n.add(id);
      return n;
    });

  return (
    <div className="ph-map">
      <div className="ph-folders" role="tablist" aria-label="Folders">
        {ordered.map((cl) => (
          <button
            key={cl.key}
            role="tab"
            aria-selected={cl.key === folder}
            style={hue(cl.key)}
            title={cl.path}
            onClick={() => setChosen(cl.key)}
          >
            <i aria-hidden="true" />
            {cl.label}
            {needCounts[cl.key] > 0 && <span className="need">● {needCounts[cl.key]}</span>}
          </button>
        ))}
      </div>
      <div className="ph-board-scroll">
        <div className="ph-board" ref={board} style={{ height: lay.height, ...(folder ? hue(folder) : {}) }}>
          <svg className="ph-wires" width={width} height={lay.height} aria-hidden="true">
            {lay.wires.map((w, i) => (
              <g key={i} className={w.kind}>
                <path d={w.d} />
                {w.head && <path className="head" d={HEAD[w.head.dir](w.head.x, w.head.y)} />}
              </g>
            ))}
          </svg>
          {shown.map((c) => {
            const r = lay.cards[c.id];
            if (!r) return null;
            const s = state(c);
            return (
              <Card
                key={c.id}
                card={c}
                rect={r}
                state={s.state}
                inferred={s.inferred}
                now={now}
                selected={false}
                compact
                className={r.small ? "small" : undefined}
                permission={!c.parent && approvals.bySession.has(c.session_id)}
                question={!c.parent && approvals.questions.has(c.session_id)}
                onOpen={(card) => setSheet(card.id)}
                onFocus={() => undefined}
              />
            );
          })}
          {lay.folds.map((f) => (
            <button
              key={f.parent}
              className="ph-fold"
              style={{ left: f.x, top: f.y, width: f.w, height: f.h }}
              aria-expanded={f.open}
              onClick={() => toggle(f.parent)}
            >
              ✓ {f.count} done {f.open ? "▴" : "▾"}
            </button>
          ))}
        </div>
        {map.error && !data && (
          <div className="ph-empty" role="alert">
            <b>Couldn't load the map</b> {map.error}
            <button onClick={() => void map.reload()}>Try again</button>
          </div>
        )}
        {!data && !map.error && (
          <p className="ph-empty" aria-busy="true">
            Loading…
          </p>
        )}
        {data && !data.cards.length && (
          <p className="ph-empty">
            No sessions yet. Run <code>robin scan</code> to index your sessions.
          </p>
        )}
        {data && !cards.length && data.cards.length > 0 && recency !== "all" && (
          <p className="ph-empty">
            Nothing {recency === "active" ? "active" : "from today"}.{" "}
            <button className="link" onClick={() => props.onRecency(WIDER[recency].to)}>
              {WIDER[recency].label}
            </button>
          </p>
        )}
      </div>
      {sel && (
        <Sheet label={sel.title || "Session"} onClose={() => setSheet(null)}>
          <TaskPanel
            key={sel.id}
            card={sel}
            state={state(sel).state}
            session={ws.sessions[sel.session_id]}
            now={now}
            onHeight={() => undefined}
            onOpenSession={(id) => {
              setSheet(null);
              props.onOpenSession(id);
            }}
            onChanged={() => {
              void map.reload();
              props.onChanged?.();
            }}
            onClose={() => setSheet(null)}
            approvals={sel.parent ? undefined : approvals.bySession.get(sel.session_id)}
            questions={sel.parent ? undefined : approvals.questions.get(sel.session_id)}
            onAnswered={() => {
              approvals.reload();
              props.onChanged?.();
            }}
          />
        </Sheet>
      )}
    </div>
  );
}
