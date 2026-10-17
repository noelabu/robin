import { type KeyboardEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Session } from "../api";
import { ago } from "../map/time";
import { hue } from "../map/hue";
import { useTick } from "../map/useMap";
import { StateBadge } from "./StateBadge";

export const ROW = 58; // px; rows and folder headers have fixed heights so only the visible ones render
export const GROUP = 30;
const OVERSCAN = 8;

const basename = (p: string) => p.split("/").filter(Boolean).pop() ?? p;

type Entry = { kind: "group"; key: string; count: number; top: number } | { kind: "session"; i: number; top: number };

/** Sessions grouped by folder, in first-seen order (the input is most recent first). */
function grouped(sessions: Session[]) {
  const groups = new Map<string, Session[]>();
  for (const s of sessions) {
    const key = s.cluster || s.execution_cwd || "";
    groups.set(key, [...(groups.get(key) ?? []), s]);
  }
  const order: Session[] = [];
  const entries: Entry[] = [];
  const offsets: number[] = []; // session index -> top
  let top = 0;
  for (const [key, list] of groups) {
    entries.push({ kind: "group", key, count: list.length, top });
    top += GROUP;
    for (const s of list) {
      offsets.push(top);
      entries.push({ kind: "session", i: order.length, top });
      order.push(s);
      top += ROW;
    }
  }
  return { order, entries, offsets, height: top };
}

/** The first entry whose bottom is below `y` (entries are sorted by top). */
function firstVisible(entries: Entry[], y: number): number {
  let lo = 0;
  let hi = entries.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const e = entries[mid];
    if (e.top + (e.kind === "group" ? GROUP : ROW) <= y) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * The session list, grouped by folder, renders only the rows in view, so thousands of sessions stay cheap
 * (NFR-01). It is one Tab stop; arrow keys, Page Up/Down, Home and End move through every session, and the
 * focused row is always rendered so focus never falls out of the list (NFR-08).
 */
export function SessionList(props: { sessions: Session[]; selected: string | null; onSelect: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  const [cursor, setCursor] = useState(0);
  const focusCursor = useRef(false);
  const now = useTick(30_000);
  const { order, entries, offsets, height: total } = useMemo(() => grouped(props.sessions), [props.sessions]);
  const n = order.length;

  const selectedIndex = order.findIndex((s) => s.session_id === props.selected);
  useEffect(() => {
    if (selectedIndex >= 0) setCursor(selectedIndex);
  }, [selectedIndex]);
  const at = Math.min(cursor, Math.max(0, n - 1));

  useLayoutEffect(() => {
    if (!focusCursor.current) return;
    focusCursor.current = false;
    box.current?.querySelector<HTMLElement>(`[data-i="${at}"]`)?.focus();
  });

  if (!n) return <p className="empty">No sessions</p>;
  const height = box.current?.clientHeight || 600;
  const from = Math.max(0, firstVisible(entries, top) - OVERSCAN);
  const shown: Entry[] = [];
  for (let k = from; k < entries.length && entries[k].top < top + height + OVERSCAN * ROW; k++) shown.push(entries[k]);
  if (!shown.some((e) => e.kind === "session" && e.i === at)) shown.push({ kind: "session", i: at, top: offsets[at] });

  function move(i: number) {
    const k = Math.max(0, Math.min(n - 1, i));
    const el = box.current;
    if (el) {
      const y = offsets[k];
      if (y < el.scrollTop) el.scrollTop = Math.max(0, y - GROUP);
      else if (y + ROW > el.scrollTop + el.clientHeight) el.scrollTop = y + ROW - el.clientHeight;
      setTop(el.scrollTop);
    }
    setCursor(k);
    focusCursor.current = true;
  }

  function onKeyDown(e: KeyboardEvent) {
    const page = Math.max(1, Math.floor(height / ROW));
    const to: Record<string, number> = {
      ArrowDown: at + 1,
      ArrowUp: at - 1,
      PageDown: at + page,
      PageUp: at - page,
      Home: 0,
      End: n - 1,
    };
    if (!(e.key in to)) return;
    e.preventDefault();
    move(to[e.key]);
  }

  return (
    <div className="session-scroll" ref={box} onScroll={(e) => setTop(e.currentTarget.scrollTop)}>
      <ul
        className="session-list"
        aria-label={`Sessions, ${n}`}
        onKeyDown={onKeyDown}
        style={{ height: total, position: "relative" }}
      >
        {shown.map((e) => {
          if (e.kind === "group")
            return (
              <li
                key={`g:${e.key}`}
                className="session-group"
                aria-hidden="true"
                title={e.key}
                style={{ ...hue(e.key), position: "absolute", top: e.top, left: 0, right: 0, height: GROUP }}
              >
                <span>{basename(e.key) || "(no folder)"}</span>
                <span className="count">{e.count}</span>
              </li>
            );
          const s = order[e.i];
          const folder = basename(s.cluster || s.execution_cwd || "") || "(no folder)";
          const sub = s.execution_cwd && s.cluster && s.execution_cwd !== s.cluster ? basename(s.execution_cwd) : null;
          return (
            <li
              key={s.session_id}
              aria-setsize={n}
              aria-posinset={e.i + 1}
              style={{ position: "absolute", top: e.top, left: 0, right: 0, height: ROW }}
            >
              <button
                className={`session-item ${s.session_id === props.selected ? "active" : ""}`}
                aria-current={s.session_id === props.selected ? "true" : undefined}
                data-i={e.i}
                tabIndex={e.i === at ? 0 : -1}
                onFocus={() => setCursor(e.i)}
                onClick={() => props.onSelect(s.session_id)}
                title={s.execution_cwd ?? undefined}
              >
                <span className="title">{s.title || `${s.runtime} · ${s.session_id.slice(0, 8)}`}</span>
                <span className="age">{ago(s.last_activity_at ?? s.updated_at, now)}</span>
                <span className="meta">
                  <StateBadge session={s} now={now} />
                  <span>
                    {" · "}
                    {s.runtime}
                    {sub ? ` · ${sub}` : ""}
                    {s.branch ? ` · ${s.branch}` : ""}
                    {s.archived ? " · archived" : ""}
                  </span>
                  {s.shared_folder_with.length > 0 && <span className="shared"> · shared folder</span>}
                </span>
                <span className="sr-only">, in {folder}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
