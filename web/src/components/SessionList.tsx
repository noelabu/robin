import { type KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Session } from "../api";
import { StateBadge } from "./StateBadge";

const ROW = 64; // px; rows are a fixed height so only the visible ones need rendering
const OVERSCAN = 8;

/**
 * The session list renders only the rows in view, so thousands of sessions stay cheap (NFR-01).
 * It is one Tab stop; arrow keys, Page Up/Down, Home and End move through every session, and the
 * focused row is always rendered so focus never falls out of the list (NFR-08).
 */
export function SessionList(props: { sessions: Session[]; selected: string | null; onSelect: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  const [cursor, setCursor] = useState(0);
  const focusCursor = useRef(false);
  const n = props.sessions.length;

  const selectedIndex = props.sessions.findIndex((s) => s.session_id === props.selected);
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
  const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const last = Math.min(n, Math.ceil((top + height) / ROW) + OVERSCAN);
  const rows = Array.from({ length: last - first }, (_, j) => first + j);
  if (at < first || at >= last) rows.push(at);

  function move(i: number) {
    const k = Math.max(0, Math.min(n - 1, i));
    const el = box.current;
    if (el) {
      if (k * ROW < el.scrollTop) el.scrollTop = k * ROW;
      else if ((k + 1) * ROW > el.scrollTop + el.clientHeight) el.scrollTop = (k + 1) * ROW - el.clientHeight;
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
        style={{ height: n * ROW, position: "relative" }}
      >
        {rows.map((i) => {
          const s = props.sessions[i];
          return (
            <li
              key={s.session_id}
              aria-setsize={n}
              aria-posinset={i + 1}
              style={{ position: "absolute", top: i * ROW, left: 0, right: 0, height: ROW }}
            >
              <button
                className={`session-item ${s.session_id === props.selected ? "active" : ""}`}
                aria-current={s.session_id === props.selected ? "true" : undefined}
                data-i={i}
                tabIndex={i === at ? 0 : -1}
                onFocus={() => setCursor(i)}
                onClick={() => props.onSelect(s.session_id)}
              >
                <span className="runtime">{s.runtime}</span>
                <StateBadge session={s} />
                <span className="cwd" title={s.execution_cwd ?? ""}>
                  {s.execution_cwd?.split("/").filter(Boolean).pop() ?? "—"}
                  {s.branch ? ` · ${s.branch}` : ""}
                  {s.archived ? " · archived" : ""}
                  {s.shared_folder_with.length > 0 && <span className="shared"> · shared folder</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
