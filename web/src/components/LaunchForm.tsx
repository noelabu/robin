import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Session, type Task } from "../api";

const SKIPS = new Set(["claude", "codex"]); // runtimes that can run without permission prompts

function overlaps(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\/+$/, "");
  const [x, y] = [norm(a), norm(b)];
  return x === y || x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}

/** Launch a session, optionally for a task (its project folder is used). Warns before sharing a folder. */
export function LaunchForm(props: {
  task: Task | null;
  defaultCwd: string;
  writers: Session[];
  onCancel: () => void;
  onLaunched: (sessionId: string, warnings: string[]) => void;
}) {
  const [runtimes, setRuntimes] = useState<string[]>([]);
  const [runtime, setRuntime] = useState("claude");
  const [cwd, setCwd] = useState(props.defaultCwd);
  const [prompt, setPrompt] = useState("");
  const [independent, setIndependent] = useState(false);
  const [worktree, setWorktree] = useState(false);
  const [skipPermissions, setSkipPermissions] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    first.current?.focus();
    void api.adapters().then((r) => setRuntimes(r.adapters.filter((a) => a.level === "live").map((a) => a.runtime)));
  }, []);

  const sharing = cwd.trim() ? props.writers.filter((s) => s.execution_cwd && overlaps(s.execution_cwd, cwd.trim())) : [];

  return (
    <form
      className="launch"
      aria-labelledby="launch-title"
      onKeyDown={(e) => e.key === "Escape" && props.onCancel()}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const r = await api.launch(
            runtime,
            props.task ? null : cwd.trim(),
            prompt.trim() || null,
            props.task?.task_id ?? null,
            independent,
            worktree,
            skipPermissions && SKIPS.has(runtime),
          );
          props.onLaunched(r.session_id, r.warnings);
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Launch failed");
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2 id="launch-title">{props.task ? `Launch a session for “${props.task.title}”` : "Launch a session"}</h2>
      <label htmlFor="rt">Runtime</label>
      <select id="rt" ref={first} value={runtime} onChange={(e) => setRuntime(e.target.value)}>
        {(runtimes.length ? runtimes : ["claude", "codex", "pi"]).map((r) => (
          <option key={r}>{r}</option>
        ))}
      </select>
      <label htmlFor="cwd">Project folder (absolute path)</label>
      <input
        id="cwd"
        value={cwd}
        onChange={(e) => setCwd(e.target.value)}
        placeholder="/Users/you/projects/app"
        required={!props.task}
        disabled={!!props.task}
      />
      {sharing.length > 0 && !worktree && (
        <p className="hint warn" role="status">
          {sharing.length === 1 ? "A session" : `${sharing.length} sessions`} may already be editing this folder (
          {sharing.map((s) => s.runtime).join(", ")}). Changes can collide.
        </p>
      )}
      <label className="inline check">
        <input type="checkbox" checked={worktree} onChange={(e) => setWorktree(e.target.checked)} /> Run in a new Git
        worktree on its own branch (keeps it apart from other sessions in this repository)
      </label>
      <label className="inline check">
        <input type="checkbox" checked={independent} onChange={(e) => setIndependent(e.target.checked)} /> Independent
        review: this session can&apos;t read other agents&apos; findings
      </label>
      {independent && runtime === "pi" && (
        <p className="hint warn" role="status">
          Pi has no sandbox, so this review will be labelled not isolated.
        </p>
      )}
      {SKIPS.has(runtime) && (
        <label className="inline check">
          <input type="checkbox" checked={skipPermissions} onChange={(e) => setSkipPermissions(e.target.checked)} />{" "}
          Skip permission prompts: the agent runs tools without asking you
        </label>
      )}
      {skipPermissions && SKIPS.has(runtime) && (
        <p className="hint warn" role="status">
          Deny rules and the sandbox still apply.
        </p>
      )}
      <label htmlFor="prompt">First instruction (optional)</label>
      <textarea id="prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || (!props.task && !cwd.trim())}>
          {busy ? "Launching…" : "Launch"}
        </button>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
