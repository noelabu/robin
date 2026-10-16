import { useEffect, useState } from "react";
import { api, type Run, type RunGit } from "../api";

const short = (sha: string | null) => (sha ? sha.slice(0, 10) : "no commits yet");

/** Branch, base commit and start state: unknown when Robin couldn't read the start, never guessed. */
function startLine(g: RunGit): string {
  const branch = g.branch ?? "detached";
  if (g.dirty_at_start === null) return `${branch} · start state unknown`;
  const dirty = g.dirty_at_start ? `${g.dirty_at_start} uncommitted file(s) at start` : "clean at start";
  return `${branch} at ${short(g.base_commit)} · ${dirty}`;
}

/** Each run's Git context (FR-050): base commit, working-tree state, and the files it changed. */
export function Runs(props: { sessionId: string; updatedAt: string }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
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
                    {r.git.changed_files.map((f) => (
                      <li key={f.path}>
                        <code>{f.path}</code> {f.change}
                      </li>
                    ))}
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
