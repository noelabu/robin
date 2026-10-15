import { useEffect, useState } from "react";
import { api, ApiError, type PrunePlan, type StorageUsage } from "../api";

const gb = (n: number) => `${(n / 1024 ** 3).toFixed(2)} GB`;

/** Storage use against the cap, a cleanup preview before anything is pruned, and history watching. */
export function SettingsPanel(props: { onClose: () => void; onChanged: () => void }) {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [plan, setPlan] = useState<PrunePlan | null>(null);
  const [watching, setWatching] = useState<boolean | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = () => {
    void api.storage().then(setUsage);
    void api.watch().then((w) => setWatching(w.enabled));
  };
  useEffect(load, []);

  async function run(label: string, fn: () => Promise<string | void>) {
    setStatus(`${label}…`);
    try {
      setStatus((await fn()) ?? null);
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label}: ${e.message}` : `${label} failed`);
    }
    load();
    props.onChanged();
  }

  return (
    <section className="detail settings" aria-labelledby="settings-title">
      <h2 id="settings-title">Settings</h2>
      <h3>Storage</h3>
      {usage && (
        <>
          <p>
            Copied activity uses <strong>{gb(usage.counted)}</strong> of {gb(usage.cap)} (
            {Math.round(usage.ratio * 100)}%){usage.state !== "ok" && <span className="warn-text"> · {usage.state}</span>}
          </p>
          <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(usage.ratio * 100)}
            aria-label="Storage used">
            <span style={{ width: `${Math.min(100, usage.ratio * 100)}%` }} />
          </div>
          <p className="hint">
            Findings, packets and manifests ({gb(usage.protected)}) are kept and never count toward the cap. Above
            it, the oldest copied activity is pruned first; pruned events stay listed but their content is gone.
          </p>
        </>
      )}
      <div className="actions">
        <button
          onClick={() =>
            void run("Preview", async () => {
              setPlan(await api.prunePreview());
            })
          }
        >
          Preview cleanup
        </button>
      </div>
      {plan && (
        <div className="cleanup">
          {plan.events ? (
            <>
              <p>
                Would prune {plan.events.toLocaleString()} oldest events (about {gb(plan.approx_bytes)}) from{" "}
                {plan.sessions} sessions, {plan.oldest?.slice(0, 10)} to {plan.newest?.slice(0, 10)}.
              </p>
              <button
                className="danger"
                onClick={() =>
                  void run("Prune", async () => {
                    const r = await api.prune();
                    setPlan(null);
                    return `Pruned ${r.pruned_events.toLocaleString()} events`;
                  })
                }
              >
                Prune them
              </button>
            </>
          ) : (
            <p>Nothing to prune: already under the cap.</p>
          )}
        </div>
      )}
      <h3>History</h3>
      <label className="inline check">
        <input
          type="checkbox"
          checked={!!watching}
          onChange={(e) =>
            void run("Saving", async () => {
              await api.setWatch(e.target.checked);
            })
          }
        />{" "}
        Follow Claude Code, Codex and Pi history as it grows (Robin reads their transcripts continuously)
      </label>
      <div className="actions">
        <button onClick={props.onClose}>Close</button>
      </div>
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
    </section>
  );
}
