import { useEffect, useState } from "react";
import { api, ApiError, type Worktree, type WorktreeStatus } from "../api";

/** The session's worktree, and removing it (FR-051, TR-072): what it would lose, then a confirmed removal. */
export function WorktreePanel(props: { sessionId: string; connected: boolean; updatedAt: string }) {
  const [wt, setWt] = useState<Worktree | null>(null);
  const [check, setCheck] = useState<WorktreeStatus | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    void api
      .worktrees(props.sessionId)
      .then((r) => setWt(r.worktrees[0] ?? null))
      .catch(() => {});
  }, [props.sessionId, props.updatedAt]);

  if (!wt) return null;

  async function remove(discard: boolean) {
    if (!wt) return;
    setStatus("Removing…");
    try {
      const r = await api.removeWorktree(wt.worktree_id, discard);
      setWt({ ...wt, removed_at: r.removed_at });
      setCheck(null);
      setStatus(`Removed. Branch ${r.branch_kept} is kept.`);
    } catch (e) {
      setStatus(e instanceof ApiError ? `Not removed: ${e.message}` : "Not removed");
    }
  }

  return (
    <section className="worktree" aria-labelledby="worktree-title">
      <h3 id="worktree-title">Worktree</h3>
      <p>
        <code>{wt.path}</code> on <code>{wt.branch}</code>, from {wt.from_branch ?? "a detached HEAD"} at{" "}
        {wt.base_commit.slice(0, 10)}
        {wt.removed_at && " · removed (the branch is kept)"}
      </p>
      {!wt.removed_at && !check && (
        <button
          disabled={props.connected}
          title={props.connected ? "Stop the session first" : undefined}
          onClick={() => void api.worktree(wt.worktree_id).then(setCheck)}
        >
          Remove worktree…
        </button>
      )}
      {props.connected && !wt.removed_at && <p className="hint">Stop the session before removing its worktree.</p>}
      {check && (
        <div className="confirm" role="group" aria-label="Remove worktree">
          {check.uncommitted.length > 0 ? (
            <p className="hint warn">
              {check.uncommitted.length} uncommitted file(s) will be lost: {check.uncommitted.slice(0, 10).join(", ")}
            </p>
          ) : (
            <p>No uncommitted files.</p>
          )}
          {check.unmerged_commits ? (
            <p>
              {check.unmerged_commits} commit(s) aren&apos;t on {check.from_branch}; the branch {check.branch} keeps them.
            </p>
          ) : null}
          <button className="danger" onClick={() => void remove(check.uncommitted.length > 0)}>
            {check.uncommitted.length > 0 ? "Discard changes and remove" : "Remove"}
          </button>
          <button onClick={() => setCheck(null)}>Cancel</button>
        </div>
      )}
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
    </section>
  );
}
