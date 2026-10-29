import { useEffect, useState } from "react";
import { api, type Run, type RunGit } from "../api";
import { FileDiff } from "./Diff";

const short = (sha: string | null) => (sha ? sha.slice(0, 10) : "no commits yet");

/** Branch, base commit and start state: unknown when Robin couldn't read the start, never guessed. */
function startLine(g: RunGit): string {
  const branch = g.branch ?? "detached";
  if (g.dirty_at_start === null) return `${branch} · start state unknown`;
  const dirty = g.dirty_at_start ? `${g.dirty_at_start} uncommitted file(s) at start` : "clean at start";
  return `${branch} at ${short(g.base_commit)} · ${dirty}`;
}

/** Each run's Git context (FR-050): base commit, working-tree state, and the files it changed; a file opens its diff. */
export function Runs(props: { sessionId: string; updatedAt: string; compact?: boolean }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [shown, setShown] = useState<string | null>(null); // run id and path
  useEffect(() => {
    let live = true;
    void api.runs(props.sessionId).then((r) => live && setRuns(r.runs)).catch(() => {});
    return () => {
      live = false;
    };
  }, [props.sessionId, props.updatedAt]);

  if (!runs?.length) return null;
  return (
    <section className="runs" aria-labelledby="runs-title">
      <h3 id="runs-title">Runs</h3>
      <ol>
        {runs.map((r) => (
          <li key={r.run_id}>
            <p>
              {new Date(r.started_at).toLocaleString()} · {r.ended_at ? (r.outcome ?? "ended") : "running"}
            </p>
            {r.git ? (
              <>
                <p className="muted">
                  {startLine(r.git)}
                  {r.git.end_commit && r.git.end_commit !== r.git.base_commit && ` · now at ${short(r.git.end_commit)}`}
                </p>
                {r.git.changed_files === null ? (
                  <p className="hint">
                    {r.ended_at
                      ? "Changed files unknown: Robin couldn't read this run's Git state at its start or end."
                      : "Changed files are listed when the run ends."}
                  </p>
                ) : r.git.changed_files.length ? (
                  <ul className="changed">
                    {r.git.changed_files.map((f) => {
                      const id = `${r.run_id}\0${f.path}`;
                      return (
                        <li key={f.path}>
                          <button
                            type="button"
                            className="link"
                            aria-expanded={props.compact ? undefined : shown === id}
                            onClick={() => setShown(shown === id ? null : id)}
                          >
                            <code>{f.path}</code>
                          </button>{" "}
                          {f.change}
                          {shown === id && (
                            <FileDiff
                              path={f.path}
                              compact={props.compact}
                              load={() => api.runDiff(props.sessionId, r.run_id, f.path)}
                              onClose={() => setShown(null)}
                            />
                          )}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="muted">No files changed.</p>
                )}
                {r.git.truncated && <p className="hint">Only the first 1,000 files are listed.</p>}
                {r.git.start_late && r.git.changed_files !== null && (
                  <p className="hint warn">Robin read the start state late; earlier changes in this run may be missing.</p>
                )}
                {r.git.shared_folder && (
                  <p className="hint warn">Another session worked in this folder during the run; some changes may be theirs.</p>
                )}
              </>
            ) : (
              <p className="muted">Not in a Git repository.</p>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
