import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";

export function LaunchForm(props: { onCancel: () => void; onLaunched: (sessionId: string) => void }) {
  const [runtimes, setRuntimes] = useState<string[]>([]);
  const [runtime, setRuntime] = useState("claude");
  const [cwd, setCwd] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    first.current?.focus();
    void api.adapters().then((r) => setRuntimes(r.adapters.filter((a) => a.level === "live").map((a) => a.runtime)));
  }, []);

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
          const r = await api.launch(runtime, cwd.trim(), prompt.trim() || null);
          props.onLaunched(r.session_id);
        } catch (err) {
          setError(err instanceof ApiError ? err.message : "Launch failed");
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2 id="launch-title">Launch a session</h2>
      <label htmlFor="rt">Runtime</label>
      <select id="rt" ref={first} value={runtime} onChange={(e) => setRuntime(e.target.value)}>
        {(runtimes.length ? runtimes : ["claude", "codex", "pi"]).map((r) => (
          <option key={r}>{r}</option>
        ))}
      </select>
      <label htmlFor="cwd">Project folder (absolute path)</label>
      <input id="cwd" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/Users/you/projects/app" required />
      <label htmlFor="prompt">First instruction (optional)</label>
      <textarea id="prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || !cwd.trim()}>
          {busy ? "Launching…" : "Launch"}
        </button>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
