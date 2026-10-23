import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type NotificationKind,
  type NotificationSettings,
  type PrunePlan,
  type RemoteSettings,
  type StorageUsage,
} from "../api";
import type { ThemeChoice } from "../theme";

const NOTIFY: [NotificationKind, string][] = [
  ["input", "A session needs your input"],
  ["failed", "A run fails"],
  ["finished", "A run finishes"],
];

const THEMES: [ThemeChoice, string][] = [
  ["system", "System"],
  ["light", "Light"],
  ["dark", "Dark"],
];

const gb = (n: number) => `${(n / 1024 ** 3).toFixed(2)} GB`;

/** Appearance, storage use against the cap, a cleanup preview before anything is pruned, and history watching. */
export function SettingsPanel(props: {
  theme: ThemeChoice;
  onTheme: (t: ThemeChoice) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [plan, setPlan] = useState<PrunePlan | null>(null);
  const [watching, setWatching] = useState<boolean | null>(null);
  const [notify, setNotify] = useState<NotificationSettings | null>(null);
  const [waitMin, setWaitMin] = useState<string | null>(null);
  const [remote, setRemote] = useState<RemoteSettings | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = () => {
    void api.storage().then(setUsage);
    void api.watch().then((w) => setWatching(w.enabled));
    void api.notifications().then(setNotify);
    void api.questionWait().then((q) => setWaitMin(String(q.wait_s / 60)));
    void api.remote().then(setRemote);
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
      <h2 id="settings-title" tabIndex={-1} ref={(h) => h?.focus()}>
        Settings
      </h2>
      <h3>Appearance</h3>
      <fieldset className="notify theme">
        <legend className="sr-only">Appearance</legend>
        {THEMES.map(([value, label]) => (
          <label key={value} className="inline check">
            <input
              type="radio"
              name="theme"
              value={value}
              checked={props.theme === value}
              onChange={() => props.onTheme(value)}
            />{" "}
            {label}
          </label>
        ))}
      </fieldset>
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
      <h3>Questions</h3>
      {waitMin !== null && (
        <label className="inline">
          Wait for an answer in Robin before a terminal session asks (minutes, 0 = off){" "}
          <input
            type="number"
            min={0}
            max={9}
            step={0.5}
            value={waitMin}
            onChange={(e) => setWaitMin(e.target.value)}
            onBlur={() =>
              void run("Saving", async () => {
                await api.setQuestionWait(Math.round(Math.min(9, Math.max(0, Number(waitMin) || 0)) * 60));
              })
            }
          />
        </label>
      )}
      <h3>Notifications</h3>
      {notify && (
        <fieldset className="notify">
          <legend className="sr-only">Notifications</legend>
          <label className="inline check">
            <input
              type="checkbox"
              checked={notify.enabled}
              onChange={(e) =>
                void run("Saving", async () => {
                  await api.setNotifications({ ...notify, enabled: e.target.checked });
                })
              }
            />{" "}
            Show macOS notifications when Robin's sessions need you (never for the session on screen)
          </label>
          {NOTIFY.map(([kind, label]) => (
            <label key={kind} className="inline check">
              <input
                type="checkbox"
                disabled={!notify.enabled}
                checked={notify.kinds.includes(kind)}
                onChange={(e) =>
                  void run("Saving", async () => {
                    const kinds = e.target.checked
                      ? [...notify.kinds, kind]
                      : notify.kinds.filter((k) => k !== kind);
                    await api.setNotifications({ enabled: notify.enabled, kinds });
                  })
                }
              />{" "}
              {label}
            </label>
          ))}
        </fieldset>
      )}
      <h3>Phone</h3>
      {remote &&
        (remote.listening ? (
          <>
            <p>
              On: open <code>{remote.urls[0]}</code> on any device signed in to Tailscale as {remote.owner}.
            </p>
            <p className="hint">
              In Safari: Share → Add to Home Screen. Other Tailscale users and shared devices are refused.
            </p>
          </>
        ) : (
          <p className="hint">
            {remote.enabled
              ? `On, waiting for Tailscale: ${remote.problem ?? "starting"}`
              : "Run `robin remote on` to open Robin from your phone over Tailscale."}
          </p>
        ))}
      <div className="actions">
        <button onClick={props.onClose}>Close</button>
      </div>
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
    </section>
  );
}
