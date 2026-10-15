import { useState } from "react";
import { api, ApiError, type Project, type Task } from "../api";
import type { Scope } from "../scope";

/** Projects and their tasks. One click scopes the session list, feed and detail to it. */
export function Navigator(props: {
  projects: Project[];
  tasks: Task[];
  scope: Scope;
  showArchived: boolean;
  onScope: (scope: Scope) => void;
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const tasksOf = (pid: string) =>
    props.tasks.filter((t) => t.project_id === pid && (props.showArchived || !t.archived));
  const is = (s: Scope) => JSON.stringify(s) === JSON.stringify(props.scope);

  return (
    <div className="navigator">
      <ul className="nav-list">
        <li>
          <button className={`nav-item ${is({ kind: "all" }) ? "active" : ""}`} onClick={() => props.onScope({ kind: "all" })}>
            All sessions
          </button>
        </li>
        {props.projects.map((p) => (
          <li key={p.project_id}>
            <button
              className={`nav-item ${is({ kind: "project", id: p.project_id }) ? "active" : ""}`}
              aria-current={is({ kind: "project", id: p.project_id }) ? "true" : undefined}
              title={p.path}
              onClick={() => props.onScope({ kind: "project", id: p.project_id })}
            >
              <span className="name">{p.name}</span>
              {p.git?.branch && <span className="branch">{p.git.branch}</span>}
            </button>
            {tasksOf(p.project_id).length > 0 && (
              <ul className="nav-tasks">
                {tasksOf(p.project_id).map((t) => (
                  <li key={t.task_id}>
                    <button
                      className={`nav-item task ${is({ kind: "task", id: t.task_id }) ? "active" : ""}`}
                      aria-current={is({ kind: "task", id: t.task_id }) ? "true" : undefined}
                      onClick={() => props.onScope({ kind: "task", id: t.task_id })}
                    >
                      <span className="name">{t.title}</span>
                      <span className={`task-status status-${t.status}`}>{t.archived ? "archived" : t.status}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      {adding ? (
        <form
          className="add-project"
          onSubmit={async (e) => {
            e.preventDefault();
            setError(null);
            try {
              const p = await api.createProject(path.trim());
              setAdding(false);
              setPath("");
              props.onChanged();
              props.onScope({ kind: "project", id: p.project_id });
            } catch (err) {
              setError(err instanceof ApiError ? err.message : "Couldn't add the project");
            }
          }}
        >
          <label htmlFor="project-path">Project folder</label>
          <input id="project-path" autoFocus value={path} onChange={(e) => setPath(e.target.value)} placeholder="/Users/you/code/app" />
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="actions">
            <button type="submit" className="primary" disabled={!path.trim()}>
              Add
            </button>
            <button type="button" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button className="link" onClick={() => setAdding(true)}>
          + Add project
        </button>
      )}
    </div>
  );
}
