import { useEffect, useMemo, useRef, useState } from "react";
import { api, signIn, type Session } from "./api";
import { ActivityFeed, type FeedScope } from "./components/ActivityFeed";
import { AttentionPanel } from "./components/AttentionPanel";
import { LaunchForm } from "./components/LaunchForm";
import { Navigator } from "./components/Navigator";
import { NewTaskForm } from "./components/NewTaskForm";
import { SearchBox } from "./components/SearchBox";
import { SessionDetail } from "./components/SessionDetail";
import { SettingsPanel } from "./components/SettingsPanel";
import { SessionList } from "./components/SessionList";
import { TaskDetail } from "./components/TaskDetail";
import { MapView } from "./map/MapView";
import { inScope, type Scope } from "./scope";
import { useWorkspace } from "./useWorkspace";

const ACTIVE_FIRST = (a: Session, b: Session) => b.updated_at.localeCompare(a.updated_at);
const VIEW_KEY = "robin.view";
const PROJECTS_KEY = "robin.projects";

type View = "map" | "sessions";

function savedView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "sessions" ? "sessions" : "map";
  } catch {
    return "map";
  }
}

function ViewTabs({ view, onView }: { view: View; onView: (v: View) => void }) {
  return (
    <div className="view-tabs" role="tablist" aria-label="View">
      {(["map", "sessions"] as const).map((v) => (
        <button key={v} role="tab" aria-selected={view === v} onClick={() => onView(v)}>
          {v === "map" ? "Map" : "Sessions"}
        </button>
      ))}
    </div>
  );
}

export function App() {
  const [auth, setAuth] = useState<"checking" | "ok" | "signed_out">("checking");
  const [view, setViewState] = useState<View>(savedView);
  const [projectsOpen, setProjectsOpen] = useState(() => {
    try {
      return localStorage.getItem(PROJECTS_KEY) === "open";
    } catch {
      return false;
    }
  });
  const rememberProjects = (open: boolean) => {
    setProjectsOpen(open);
    try {
      localStorage.setItem(PROJECTS_KEY, open ? "open" : "closed");
    } catch {
      /* not remembered */
    }
  };
  const setView = (v: View) => {
    setViewState(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* the choice just isn't remembered */
    }
  };
  const [scope, setScope] = useState<Scope>({ kind: "all" });
  const [selected, setSelected] = useState<string | null>(null);
  const [panel, setPanel] = useState<"none" | "launch" | "task" | "settings">("none");
  // Keyboard focus goes back to whatever opened a panel when it closes (NFR-08).
  const opener = useRef<HTMLElement | null>(null);
  const openPanel = (p: "launch" | "task" | "settings") => {
    opener.current = document.activeElement as HTMLElement | null;
    setPanel(p);
  };
  const closePanel = () => {
    setPanel("none");
    requestAnimationFrame(() => opener.current?.focus());
  };
  const openSession = (id: string) => {
    setSelected(id);
    requestAnimationFrame(() => document.getElementById("detail-title")?.focus());
  };
  const [storage, setStorage] = useState<{ state: string; ratio: number } | null>(null);
  const [runtimeFilter, setRuntimeFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const ws = useWorkspace(auth === "ok");

  useEffect(() => {
    void signIn().then((ok) => setAuth(ok ? "ok" : "signed_out"));
  }, []);

  useEffect(() => {
    if (auth !== "ok") return;
    const check = () => void api.storage().then(setStorage).catch(() => undefined);
    check();
    const t = window.setInterval(check, 60_000);
    return () => window.clearInterval(t);
  }, [auth]);

  const task = scope.kind === "task" ? ws.tasks.find((t) => t.task_id === scope.id) ?? null : null;
  const project =
    scope.kind === "project"
      ? ws.projects.find((p) => p.project_id === scope.id)
      : task
        ? ws.projects.find((p) => p.project_id === task.project_id)
        : undefined;

  const all = useMemo(() => Object.values(ws.sessions).sort(ACTIVE_FIRST), [ws.sessions]);
  // Like the server's count: observed sessions that aren't archived.
  const observedLoaded = all.filter((s) => s.mode === "observed" && !s.archived).length;
  const runtimes = useMemo(() => [...new Set(all.map((s) => s.runtime))].sort(), [all]);
  const sessions = all.filter(
    (s) => inScope(s, scope, null) && (showArchived || !s.archived) && (!runtimeFilter || s.runtime === runtimeFilter),
  );
  const writers = all.filter((s) => !s.archived && !["stopped", "failed"].includes(s.runtime_state) && s.mode === "managed");
  const current: Session | undefined = selected
    ? ws.sessions[selected]
    : scope.kind === "task"
      ? undefined
      : sessions[0];

  // The session on screen, so the service keeps its notifications quiet while the dashboard is visible.
  const shown = auth === "ok" && view === "sessions" ? (current?.session_id ?? null) : null;
  useEffect(() => {
    if (auth !== "ok") return;
    const report = () => void api.presence(document.visibilityState === "visible" ? shown : null).catch(() => {});
    report();
    const timer = window.setInterval(report, 20_000);
    document.addEventListener("visibilitychange", report);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", report);
    };
  }, [auth, shown]);

  const choose = (next: Scope) => {
    setScope(next);
    setSelected(null);
    setPanel("none");
  };
  const feedScope: FeedScope = current
    ? { session_id: current.session_id }
    : scope.kind === "task"
      ? { task_id: scope.id }
      : scope.kind === "project"
        ? { project_id: scope.id }
        : {};

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

  const tabs = <ViewTabs view={view} onView={setView} />;
  if (view === "map")
    return (
      <MapView
        ws={ws}
        tabs={tabs}
        onOpenSession={(id) => {
          setView("sessions");
          setScope({ kind: "all" });
          setPanel("none");
          openSession(id);
          void ws.ensure(id).catch(() => setNotice("That session couldn't be loaded."));
        }}
        onOpenAttention={() => setView("sessions")}
        onChanged={() => void ws.reload()}
      />
    );

  return (
    <div className="shell">
      <header className="topbar">
        <h1 className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#111" strokeWidth="2.4" strokeLinecap="round">
              <path d="M5 19 12 5l7 14M8.5 13h7" />
            </svg>
          </span>
          Robin
        </h1>
        {tabs}
        <SearchBox
          onOpenSession={(id) => {
            setPanel("none");
            openSession(id);
          }}
          onOpenTask={(id) => choose({ kind: "task", id })}
        />
        <span className="spacer" />
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
        {scope.kind === "project" && project && (
          <button onClick={() => openPanel("task")}>New task</button>
        )}
        {storage && storage.state !== "ok" && (
          <button className={`pill ${storage.state === "over" ? "bad" : "warn"}`} onClick={() => openPanel("settings")}>
            Storage {Math.round(storage.ratio * 100)}%
          </button>
        )}
        <button onClick={() => openPanel("settings")}>Settings</button>
        <button className="primary" onClick={() => openPanel("launch")}>
          Launch session
        </button>
      </header>
      <nav className="sidebar" aria-label="Projects and sessions">
        <details
          className="projects"
          open={projectsOpen || scope.kind !== "all"}
          onToggle={(e) => rememberProjects(e.currentTarget.open)}
        >
          <summary>Projects and tasks</summary>
          <Navigator
            projects={ws.projects}
            tasks={ws.tasks}
            scope={scope}
            showArchived={showArchived}
            onScope={choose}
            onChanged={() => void ws.reload()}
          />
        </details>
        <div className="filters">
          <label className="inline">
            Runtime
            <select value={runtimeFilter} onChange={(e) => setRuntimeFilter(e.target.value)}>
              <option value="">all</option>
              {runtimes.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <label className="inline">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> Show
            archived
          </label>
        </div>
        <SessionList sessions={sessions} selected={current?.session_id ?? null} onSelect={setSelected} />
        {!ws.olderDone && observedLoaded < ws.observedTotal && (
          <button className="link" onClick={() => void ws.loadOlder()}>
            {observedLoaded.toLocaleString()} of {ws.observedTotal.toLocaleString()} observed sessions loaded · load
            older
          </button>
        )}
      </nav>
      <main className="main">
        <AttentionPanel refreshKey={ws.attentionVersion} onOpenSession={openSession} />
        {notice && (
          <p className="hint warn" role="status">
            {notice}
          </p>
        )}
        {panel === "launch" && (
          <LaunchForm
            task={task}
            defaultCwd={project?.path ?? ""}
            writers={writers}
            onCancel={closePanel}
            onLaunched={(sid, warnings) => {
              setPanel("none");
              setNotice(warnings.length ? warnings.join(" ") : null);
              openSession(sid);
            }}
          />
        )}
        {panel === "settings" && (
          <SettingsPanel
            onClose={closePanel}
            onChanged={() => void api.storage().then(setStorage).catch(() => undefined)}
          />
        )}
        {panel === "task" && project && (
          <NewTaskForm
            project={project}
            onCancel={closePanel}
            onCreated={(t) => {
              void ws.reload();
              choose({ kind: "task", id: t.task_id });
            }}
          />
        )}
        {task && !selected && (
          <TaskDetail
            taskId={task.task_id}
            project={project}
            refreshKey={ws.attentionVersion}
            sessions={all}
            onOpenSession={openSession}
            onLaunch={() => openPanel("launch")}
            onChanged={() => void ws.reload()}
          />
        )}
        {current ? (
          <SessionDetail key={current.session_id} session={current} onChanged={() => void ws.reload()} />
        ) : (
          panel === "none" &&
          !task && (
            <p className="empty">
              {all.length ? "No sessions here." : "No sessions yet. Launch one, or add a project to get started."}
            </p>
          )
        )}
        <ActivityFeed items={ws.feed} scope={feedScope} sessions={ws.sessions} ready={ws.ready} />
      </main>
    </div>
  );
}
