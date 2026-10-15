import { useEffect, useMemo, useState } from "react";
import { signIn, type Session } from "./api";
import { ActivityFeed } from "./components/ActivityFeed";
import { LaunchForm } from "./components/LaunchForm";
import { SessionDetail } from "./components/SessionDetail";
import { SessionList } from "./components/SessionList";
import { useWorkspace } from "./useWorkspace";

export function App() {
  const [auth, setAuth] = useState<"checking" | "ok" | "signed_out">("checking");
  const [selected, setSelected] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const ws = useWorkspace(auth === "ok");

  useEffect(() => {
    void signIn().then((ok) => setAuth(ok ? "ok" : "signed_out"));
  }, []);

  const sessions = useMemo(
    () => Object.values(ws.sessions).sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [ws.sessions],
  );
  const current: Session | undefined = selected ? ws.sessions[selected] : sessions[0];

  if (auth === "checking") return <main className="center" aria-busy="true">Connecting…</main>;
  if (auth === "signed_out")
    return (
      <main className="center">
        <h1>Robin</h1>
        <p>This sign-in link was already used or has expired.</p>
        <p>
          Run <code>robin open</code> in a terminal for a new one.
        </p>
      </main>
    );

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">Robin</span>
        <span className={`pill ${ws.supervisorConnected ? "ok" : "warn"}`} role="status">
          Supervisor {ws.supervisorConnected ? "connected" : "unavailable"}
        </span>
        <span className={`pill ${ws.streamState === "live" ? "ok" : "warn"}`} role="status">
          Updates {ws.streamState}
        </span>
        {ws.gap && (
          <span className="pill warn" role="status" title="Some events were not observed; history may be incomplete">
            Gap in coverage
          </span>
        )}
        <button className="primary" onClick={() => setLaunching(true)}>
          Launch session
        </button>
      </header>
      <nav className="sidebar" aria-label="Sessions">
        <SessionList sessions={sessions} selected={current?.session_id ?? null} onSelect={setSelected} />
      </nav>
      <main className="main">
        {launching && (
          <LaunchForm
            onCancel={() => setLaunching(false)}
            onLaunched={(sid) => {
              setLaunching(false);
              setSelected(sid);
            }}
          />
        )}
        {current ? (
          <SessionDetail session={current} />
        ) : (
          !launching && <p className="empty">No sessions yet. Launch one to get started.</p>
        )}
        <ActivityFeed items={ws.feed} sessionId={current?.session_id ?? null} ready={ws.ready} />
      </main>
    </div>
  );
}
