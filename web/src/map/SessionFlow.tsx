import { useEffect, useMemo, useState } from "react";
import { api, type MapCard, type MapCluster, type Session } from "../api";
import { useApprovals } from "../components/Approval";
import { Card } from "./Card";
import { layout, PANEL_GAP } from "./layout";
import { TaskPanel } from "./TaskPanel";
import { Wire } from "./Wire";
import { liveState, useTick } from "./useMap";

const INSET = { x: 24, y: 44 }; // where layout puts a cluster's first card; the flow has no frame

/** One session's card and what it started (subagents, skills, workflows, shells), as on the Map; a card opens its tasks below it. */
export function SessionFlow({ session, onChanged }: { session: Session; onChanged?: () => void }) {
  const [cards, setCards] = useState<MapCard[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [panelH, setPanelH] = useState(0);
  const now = useTick();
  const sid = session.session_id;
  const approvals = useApprovals(session.updated_at);
  const waiting = approvals.bySession.get(sid);
  const asked = approvals.questions.get(sid);

  useEffect(() => {
    let live = true;
    api
      .mapCards([sid])
      .then((r) => live && setCards(r.cards))
      .catch(() => live && setCards([]));
    return () => {
      live = false;
    };
  }, [sid, session.updated_at, session.last_activity_at]);

  const lay = useMemo(() => {
    const cluster: MapCluster = {
      key: cards[0]?.cluster ?? "",
      label: "",
      path: "",
      sessions: 1,
      shown: 1,
      running: 0,
      waiting: 0,
      last_activity_at: null,
    };
    return layout([cluster.key], [cluster], cards, open && panelH ? { [open]: panelH } : {});
  }, [cards, open, panelH]);

  if (!cards.length) return null;
  const count = (k: string) => cards.filter((c) => (c.kind ?? (c.parent ? "subagent" : "session")) === k).length;
  const parts = [
    [count("skill"), "skill"],
    [count("workflow"), "workflow"],
    [count("subagent"), "subagent"],
    [count("shell"), "shell"],
  ]
    .filter(([n]) => n)
    .map(([n, w]) => `${n} ${w}${n === 1 ? "" : "s"}`);
  const sel = open ? cards.find((c) => c.id === open) : undefined;
  const at = (id: string) => {
    const r = lay.cards[id];
    return r && { ...r, x: r.x - INSET.x + 16, y: r.y - INSET.y + 16 };
  };
  const toggle = (id: string) => {
    setPanelH(0);
    setOpen(open === id ? null : id);
  };

  return (
    <div className="flow">
      <div className="flow-h">
        <span>Session{parts.length ? " & what it started" : ""}</span>
        <span>
          {parts.length ? `${parts.join(" · ")} · ` : ""}click a card for its tasks and files
        </span>
      </div>
      <div className="flow-canvas" style={{ height: lay.bounds.h - INSET.y + 8 }}>
        <svg className="map-wires" width={lay.bounds.w} height={lay.bounds.h} aria-hidden="true">
          {lay.edges.map((e) => (
            <Wire key={e.to} edge={e} transform={`translate(${16 - INSET.x} ${16 - INSET.y})`} />
          ))}
        </svg>
        {cards.map((c) => {
          const r = at(c.id);
          if (!r) return null;
          const s = liveState(c, c.parent ? undefined : session, now);
          return (
            <Card
              key={c.id}
              card={c}
              rect={r}
              state={s.state}
              inferred={s.inferred}
              now={now}
              selected={open === c.id}
              permission={!c.parent && !!waiting}
              question={!c.parent && !!asked}
              onOpen={(card) => toggle(card.id)}
              onFocus={() => {}}
            />
          );
        })}
        {sel && at(sel.id) && (
          <div
            style={{
              position: "absolute",
              left: at(sel.id)!.x,
              top: at(sel.id)!.y + at(sel.id)!.h + PANEL_GAP,
              width: Math.max(at(sel.id)!.w, 272),
            }}
          >
            <TaskPanel
              key={sel.id}
              card={sel}
              state={liveState(sel, sel.parent ? undefined : session, now).state}
              session={sel.parent ? undefined : session}
              now={now}
              onHeight={setPanelH}
              onChanged={onChanged}
              onClose={() => setOpen(null)}
              actions={false}
              approvals={sel.parent ? undefined : waiting}
              questions={sel.parent ? undefined : asked}
              onAnswered={approvals.reload}
            />
          </div>
        )}
      </div>
    </div>
  );
}
