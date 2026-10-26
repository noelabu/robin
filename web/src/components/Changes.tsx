import { useEffect, useState } from "react";
import { api, type Changes as ChangesData } from "../api";
import { FileDiff } from "./Diff";

const STATE: Record<Exclude<ChangesData["state"], "ok">, string> = {
  not_repo: "Not a Git repository.",
  missing: "The folder is gone.",
  no_folder: "No folder recorded for this session.",
  unknown: "Couldn't read Git.",
};

/** What differs from HEAD in the session folder's repository, read only while open. A row opens its diff. */
export function Changes(props: { sessionId: string; updatedAt: string; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ChangesData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let live = true;
    api
      .changes(props.sessionId)
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [open, props.sessionId, props.updatedAt]);

  return (
    <details className="facts-box changes" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Changes</summary>
      {error ? (
        <p className="hint warn">{error}</p>
      ) : !data ? (
        open && <p className="muted">Loading…</p>
      ) : data.state !== "ok" ? (
        <p className="muted">{STATE[data.state]}</p>
      ) : !data.files.length ? (
        <p className="muted">No changes.</p>
      ) : (
        <>
          <p className="muted">
            {data.branch ?? "detached"} · {data.head ? data.head.slice(0, 10) : "no commits yet"}
          </p>
          <ul className="changed">
            {data.files.map((f) => (
              <li key={f.path}>
                <button
                  type="button"
                  className="link"
                  aria-expanded={props.compact ? undefined : shown === f.path}
                  onClick={() => setShown(shown === f.path ? null : f.path)}
                >
                  <code>{f.path}</code>
                </button>{" "}
                {f.change}{" "}
                <span className="muted">
                  {f.binary ? "binary" : f.added === null ? "" : `+${f.added} −${f.removed}`}
                </span>
                {shown === f.path && (
                  <FileDiff
                    path={f.path}
                    compact={props.compact}
                    load={() => api.changesDiff(props.sessionId, f.path)}
                    onClose={() => setShown(null)}
                  />
                )}
              </li>
            ))}
          </ul>
          {data.truncated && <p className="hint">Only the first 1,000 files are listed.</p>}
        </>
      )}
    </details>
  );
}
