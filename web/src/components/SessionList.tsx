import { useRef, useState } from "react";
import type { Session } from "../api";
import { StateBadge } from "./StateBadge";

const ROW = 64; // px; rows are a fixed height so only the visible ones need rendering
const OVERSCAN = 8;

/** The session list renders only the rows in view, so thousands of sessions stay cheap (NFR-01). */
export function SessionList(props: { sessions: Session[]; selected: string | null; onSelect: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  if (!props.sessions.length) return <p className="empty">No sessions</p>;
  const height = box.current?.clientHeight ?? 600;
  const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const last = Math.min(props.sessions.length, Math.ceil((top + height) / ROW) + OVERSCAN);
  return (
    <div className="session-scroll" ref={box} onScroll={(e) => setTop(e.currentTarget.scrollTop)}>
      <ul className="session-list" style={{ height: props.sessions.length * ROW, position: "relative" }}>
        {props.sessions.slice(first, last).map((s, i) => (
          <li key={s.session_id} style={{ position: "absolute", top: (first + i) * ROW, left: 0, right: 0, height: ROW }}>
            <button
              className={`session-item ${s.session_id === props.selected ? "active" : ""}`}
              aria-current={s.session_id === props.selected ? "true" : undefined}
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
        ))}
      </ul>
    </div>
  );
}
