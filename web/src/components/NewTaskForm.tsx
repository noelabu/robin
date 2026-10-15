import { useState } from "react";
import { api, ApiError, type Project, type Task } from "../api";

export function NewTaskForm(props: { project: Project; onCancel: () => void; onCreated: (task: Task) => void }) {
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="launch"
      aria-labelledby="new-task-title"
      onKeyDown={(e) => e.key === "Escape" && props.onCancel()}
      onSubmit={async (e) => {
        e.preventDefault();
        setError(null);
        try {
          props.onCreated(await api.createTask(props.project.project_id, title.trim(), objective.trim()));
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Couldn't create the task");
        }
      }}
    >
      <h2 id="new-task-title">New task in {props.project.name}</h2>
      <label htmlFor="task-title">Title</label>
      <input id="task-title" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
      <label htmlFor="task-objective">Objective</label>
      <textarea id="task-objective" rows={3} value={objective} onChange={(e) => setObjective(e.target.value)} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={!title.trim() || !objective.trim()}>
          Create task
        </button>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
