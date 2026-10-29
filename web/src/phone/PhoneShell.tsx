import { useEffect, useState, type ReactNode } from "react";
import { api, type Session } from "../api";
import { AttentionPanel } from "../components/AttentionPanel";
import { LaunchForm } from "../components/LaunchForm";
import { RecencySwitch, WIDER } from "../components/RecencySwitch";
import { SessionList } from "../components/SessionList";
import { SettingsPanel } from "../components/SettingsPanel";
import type { Recency } from "../recency";
import type { ThemeChoice } from "../theme";
import type { useWorkspace } from "../useWorkspace";
import { PhoneMap } from "./PhoneMap";
import { Sheet } from "./Sheet";

export type PhoneTab = "inbox" | "sessions" | "map" | "settings";

const TABS: { tab: PhoneTab; label: string; icon: string }[] = [
  { tab: "inbox", label: "Inbox", icon: "◉" },
  { tab: "sessions", label: "Sessions", icon: "☰" },
  { tab: "map", label: "Map", icon: "◫" },
  { tab: "settings", label: "Settings", icon: "⚙" },
];
const NEXT: Record<Recency, Recency> = { active: "today", today: "all", all: "active" };
const LABEL: Record<Recency, string> = { active: "Active", today: "Today", all: "All" };

function Bar({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <header className="ph-bar">
      {title}
      <span className="spacer" />
      {children}
    </header>
  );
}

/** Robin on a phone: one screen at a time behind a bottom tab bar, Inbox first. */
export function PhoneShell(props: {
  ws: ReturnType<typeof useWorkspace>;
  tab: PhoneTab;
  onTab: (t: PhoneTab) => void;
  sessions: Session[];
  filtered: number;
  counts: Record<Recency, number>;
  recency: Recency;
  onRecency: (r: Recency) => void;
  selected: Session | undefined;
  onSelect: (id: string | null) => void;
  detail: ReactNode;
  writers: Session[];
  theme: ThemeChoice;
  onTheme: (t: ThemeChoice) => void;
  onStorageChanged: () => void;
  notice: string | null;
  onOpenSession: (id: string) => void;
  onLaunched: (sessionId: string, warnings: string[]) => void;
}) {
  const { ws, tab, recency } = props;
  const [waiting, setWaiting] = useState(0);
  const [launching, setLaunching] = useState(false);
  useEffect(() => {
    void api.attention().then((a) => setWaiting(a.items.length)).catch(() => undefined);
  }, [ws.attentionVersion]);

  let screen: ReactNode;
  if (tab === "inbox")
    screen = (
      <>
        <Bar
          title={
            <h1 className="brand" aria-label="Robin">
              Rob<i>i</i>n
            </h1>
          }
        >
          <span className={`ph-live ${ws.streamState === "live" ? "ok" : ""}`} role="status" title={`Updates ${ws.streamState}`} />
        </Bar>
        <div className="ph-body">
          {props.notice && (
            <p className="hint warn" role="status">
              {props.notice}
            </p>
          )}
          <AttentionPanel refreshKey={ws.attentionVersion} onOpenSession={props.onOpenSession} />
        </div>
      </>
    );
  else if (tab === "sessions" && props.selected)
    screen = (
      <>
        <Bar
          title={
            <>
              <button className="ph-icon" aria-label="Back" onClick={() => props.onSelect(null)}>
                ‹
              </button>
              <h2 className="ph-title">{props.selected.title || `${props.selected.runtime} · ${props.selected.session_id.slice(0, 8)}`}</h2>
            </>
          }
        />
        <div className="ph-body ph-detail">{props.detail}</div>
      </>
    );
  else if (tab === "sessions")
    screen = (
      <>
        <Bar title={<h2 className="ph-title">Sessions</h2>}>
          <button className="ph-icon add" aria-label="Launch session" onClick={() => setLaunching(true)}>
            ＋
          </button>
        </Bar>
        <div className="ph-body ph-list">
          {props.notice && (
            <p className="hint warn" role="status">
              {props.notice}
            </p>
          )}
          <RecencySwitch value={recency} counts={props.counts} onChange={props.onRecency} />
          {!props.sessions.length && recency !== "all" && props.filtered ? (
            <p className="empty">
              Nothing {recency === "active" ? "active" : "from today"}.{" "}
              <button className="link" onClick={() => props.onRecency(WIDER[recency].to)}>
                {WIDER[recency].label}
              </button>
            </p>
          ) : !props.sessions.length ? (
            <p className="empty">No sessions yet.</p>
          ) : (
            <SessionList sessions={props.sessions} selected={null} onSelect={props.onSelect} />
          )}
        </div>
      </>
    );
  else if (tab === "map")
    screen = (
      <>
        <Bar title={<h2 className="ph-title">Map</h2>}>
          <button
            className="ph-chip"
            aria-label={`Show sessions: ${LABEL[recency]}`}
            title="Active, Today or All"
            onClick={() => props.onRecency(NEXT[recency])}
          >
            {LABEL[recency]}
          </button>
        </Bar>
        <PhoneMap ws={ws} recency={recency} onRecency={props.onRecency} onOpenSession={props.onOpenSession} onChanged={() => void ws.reload()} />
      </>
    );
  else
    screen = (
      <>
        <Bar title={<h2 className="ph-title">Settings</h2>} />
        <div className="ph-body">
          <SettingsPanel theme={props.theme} onTheme={props.onTheme} onClose={() => props.onTab("inbox")} onChanged={props.onStorageChanged} />
        </div>
      </>
    );

  return (
    <div className="phone-app">
      <div className="ph-screen">{screen}</div>
      <nav className="ph-tabs" aria-label="Sections">
        {TABS.map((t) => (
          <button key={t.tab} aria-current={tab === t.tab ? "page" : undefined} onClick={() => props.onTab(t.tab)}>
            <span aria-hidden="true">{t.icon}</span>
            {t.label}
            {t.tab === "inbox" && waiting > 0 && (
              <i className="ph-badge" aria-label={`${waiting} waiting`}>
                {waiting}
              </i>
            )}
          </button>
        ))}
      </nav>
      {launching && (
        <Sheet label="Launch a session" onClose={() => setLaunching(false)}>
          <LaunchForm
            task={null}
            defaultCwd=""
            writers={props.writers}
            onCancel={() => setLaunching(false)}
            onLaunched={(sid, warnings) => {
              setLaunching(false);
              props.onLaunched(sid, warnings);
            }}
          />
        </Sheet>
      )}
    </div>
  );
}
