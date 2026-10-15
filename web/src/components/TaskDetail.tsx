import { useEffect, useState } from "react";
import { api, ApiError, type Project, type Session, type TaskDetail as TaskDetailT } from "../api";
import { FindingsCompare } from "./FindingsCompare";

/** A task: objective, the sessions working on it, and its status. Status changes are explicit. */
export function TaskDetail(props: {
  taskId: string;
  project: Project | undefined;
  refreshKey: number;
  sessions: Session[];
  onOpenSession: (id: string) => void;
  onLaunch: () => void;
  onChanged: () => void;
}) {
  const [task, setTask] = useState<TaskDetailT | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = () => void api.task(props.taskId).then(setTask).catch(() => setTask(null));
  useEffect(load, [props.taskId, props.refreshKey]);

  if (!task) return <section className="detail">Loading task…</section>;

  async function change(label: string, changes: Parameters<typeof api.patchTask>[1]) {
    if (!task) return;
    try {
      await api.patchTask(task, changes);
      setStatus(`${label}: done`);
      props.onChanged();
      load();
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} failed: ${e.message}` : `${label} failed`);
      load();
    }
  }

  return (
    <section className="detail" aria-labelledby="task-title">
      <h2 id="task-title">
        {task.title} <span className={`task-status status-${task.status}`}>{task.archived ? "archived" : task.status}</span>
      </h2>
      <p className="objective">{task.objective}</p>
      <dl className="facts">
        <dt>Project</dt>
        <dd>{props.project ? `${props.project.name} (${props.project.path})` : task.project_id}</dd>
        {task.completion_evidence && (
          <>
            <dt>Evidence</dt>
            <dd>{task.completion_evidence}</dd>
          </>
        )}
      </dl>
      <h3>Sessions</h3>
      {task.sessions.length ? (
        <ul className="task-sessions">
          {task.sessions.map((s) => (
            <li key={s.session_id}>
              <button className="link" onClick={() => props.onOpenSession(s.session_id)}>
                {s.runtime}
                {s.role ? ` · ${s.role}` : ""} · {s.runtime_state}
                {s.current ? "" : " · moved to another task"}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">No sessions on this task yet.</p>
      )}
      <div className="actions">
        <button className="primary" onClick={props.onLaunch} disabled={task.status !== "open"}>
          Launch session for this task
        </button>
        {task.status === "open" ? (
          <>
            <button
              onClick={() => {
                const evidence = window.prompt("What shows this task is done? (optional)") ?? undefined;
                void change("Complete", { status: "completed", completion_evidence: evidence || undefined });
              }}
            >
              Mark completed
            </button>
            <button onClick={() => void change("Abandon", { status: "abandoned" })}>Abandon</button>
          </>
        ) : (
          <button onClick={() => void change("Reopen", { status: "open" })}>Reopen</button>
        )}
        <button onClick={() => void change(task.archived ? "Restore" : "Archive", { archived: !task.archived })}>
          {task.archived ? "Restore" : "Archive"}
        </button>
      </div>
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
      <FindingsCompare
        taskId={task.task_id}
        projectId={props.project?.project_id}
        sessions={props.sessions.filter((s) => task.sessions.some((t) => t.session_id === s.session_id))}
        refreshKey={props.refreshKey}
      />
    </section>
  );
}
