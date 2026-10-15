import { useEffect, useMemo, useState } from "react";
import { signIn, type Session } from "./api";
import { ActivityFeed, type FeedScope } from "./components/ActivityFeed";
import { AttentionPanel } from "./components/AttentionPanel";
import { LaunchForm } from "./components/LaunchForm";
import { Navigator } from "./components/Navigator";
import { NewTaskForm } from "./components/NewTaskForm";
import { SessionDetail } from "./components/SessionDetail";
import { SessionList } from "./components/SessionList";
import { TaskDetail } from "./components/TaskDetail";
import { inScope, type Scope } from "./scope";
import { useWorkspace } from "./useWorkspace";

const ACTIVE_FIRST = (a: Session, b: Session) => b.updated_at.localeCompare(a.updated_at);

export function App() {
  const [auth, setAuth] = useState<"checking" | "ok" | "signed_out">("checking");
  const [scope, setScope] = useState<Scope>({ kind: "all" });
  const [selected, setSelected] = useState<string | null>(null);
  const [panel, setPanel] = useState<"none" | "launch" | "task">("none");
  const [runtimeFilter, setRuntimeFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const ws = useWorkspace(auth === "ok");

  useEffect(() => {
    void signIn().then((ok) => setAuth(ok ? "ok" : "signed_out"));
  }, []);

  const task = scope.kind === "task" ? ws.tasks.find((t) => t.task_id === scope.id) ?? null : null;
  const project =
    scope.kind === "project"
      ? ws.projects.find((p) => p.project_id === scope.id)
      : task
        ? ws.projects.find((p) => p.project_id === task.project_id)
        : undefined;

  const all = useMemo(() => Object.values(ws.sessions).sort(ACTIVE_FIRST), [ws.sessions]);
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
        {scope.kind === "project" && project && (
          <button onClick={() => setPanel("task")}>New task</button>
        )}
        <button className="primary" onClick={() => setPanel("launch")}>
          Launch session
        </button>
      </header>
      <nav className="sidebar" aria-label="Projects and sessions">
        <Navigator
          projects={ws.projects}
          tasks={ws.tasks}
          scope={scope}
          showArchived={showArchived}
          onScope={choose}
          onChanged={() => void ws.reload()}
        />
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
      </nav>
      <main className="main">
        <AttentionPanel refreshKey={ws.attentionVersion} onOpenSession={setSelected} />
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
            onCancel={() => setPanel("none")}
            onLaunched={(sid, warnings) => {
              setPanel("none");
              setNotice(warnings.length ? warnings.join(" ") : null);
              setSelected(sid);
            }}
          />
        )}
        {panel === "task" && project && (
          <NewTaskForm
            project={project}
            onCancel={() => setPanel("none")}
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
            onOpenSession={setSelected}
            onLaunch={() => setPanel("launch")}
            onChanged={() => void ws.reload()}
          />
        )}
        {current ? (
          <SessionDetail session={current} onChanged={() => void ws.reload()} />
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
