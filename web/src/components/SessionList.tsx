import type { Session } from "../api";
import { StateBadge } from "./StateBadge";

export function SessionList(props: { sessions: Session[]; selected: string | null; onSelect: (id: string) => void }) {
  if (!props.sessions.length) return <p className="empty">No sessions</p>;
  return (
    <ul className="session-list">
      {props.sessions.map((s) => (
        <li key={s.session_id}>
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
            </span>
            {s.shared_folder_with.length > 0 && (
              <span className="shared" title="Another session may be editing this folder">
                shared folder
              </span>
            )}
          </button>
        </li>
      ))}
    </ul>
  );
}
