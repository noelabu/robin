import { type KeyboardEvent, type ReactNode, useState } from "react";
import { api, ApiError, type Session } from "../api";
import { RuntimeIcon } from "../map/RuntimeIcon";
import { ago as since } from "../map/time";
import { SessionFlow } from "../map/SessionFlow";
import { ContinueDialog } from "./ContinueDialog";
import { Conversation } from "./Conversation";
import { Runs } from "./Runs";
import { WorktreePanel } from "./WorktreePanel";
import { sessionState } from "../map/useMap";
import { LABEL, StateBadge } from "./StateBadge";

function ago(iso: string | null): string {
  if (!iso) return "no evidence yet";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
}

// Like the server's check: an id that could be read as a flag is never shown as a command to run.
const NATIVE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function resumeCommand(runtime: string, id: string): string | null {
  if (!NATIVE_ID.test(id)) return null;
  if (runtime === "claude") return `claude --resume ${id}`;
  if (runtime === "codex") return `codex resume ${id}`;
  if (runtime === "pi") return `pi --session ${id}`;
  return `robin open ${id}`;
}

/** Enter sends, Shift+Enter starts a new line; a key that finishes an IME composition does neither. */
function enterSends(e: KeyboardEvent<HTMLTextAreaElement>) {
  if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
  e.preventDefault();
  e.currentTarget.form?.requestSubmit();
}

export function SessionDetail({
  session,
  onChanged,
  liveCursor,
  events,
  onOpenSession,
}: {
  session: Session;
  onChanged?: () => void;
  liveCursor?: number;
  events?: ReactNode;
  onOpenSession?: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const c = session.controls;

  async function act(body: { type: string; text?: string; interrupt_running?: boolean }, label: string) {
    setStatus(`${label}…`);
    try {
      const { action } = await api.act(session.session_id, body);
      setStatus(action.state === "failed" && action.error ? `${label} not sent: ${action.error.message}` : `${label}: ${action.state}`);
      if ((body.type === "message" || body.type === "takeover") && action.state !== "failed") setText("");
      return action;
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} not sent: ${e.message}` : `${label} failed`);
      return null;
    }
  }

  async function archive() {
    try {
      await api.archiveSession(session.session_id, !session.archived);
      setStatus(session.archived ? "Restored" : "Archived: hidden from default views, nothing deleted");
      onChanged?.();
    } catch (e) {
      setStatus(e instanceof ApiError ? `Archive failed: ${e.message}` : "Archive failed");
    }
  }

  async function handOff() {
    const running = ["running", "waiting_for_input"].includes(session.runtime_state);
    if (running && !window.confirm("A turn is running. Handing off now ends it. Continue?")) return;
    await act({ type: "handoff", interrupt_running: running }, "Hand off");
  }

  async function openTerminal() {
    setStatus("Opening Terminal…");
    try {
      await api.openTerminal(session.session_id);
      setStatus("Opened in Terminal");
    } catch (e) {
      setStatus(e instanceof ApiError ? `Couldn't open Terminal: ${e.message}` : "Couldn't open Terminal");
    }
  }

  const handedOff = session.control_owner === "terminal";
  const resume = session.native_session_id ? resumeCommand(session.runtime, session.native_session_id) : null;

  const stale = session.connection_state !== "connected" && session.mode === "managed";
  // "Take back control" only means something once the session is handed off.
  const disabledReasons = (["interrupt", "stop", "handoff", "reclaim"] as const)
    .filter((k) => k !== "reclaim" || session.control_owner === "terminal")
    .filter((k) => !c[k].enabled && c[k].reason && c[k].reason !== c.message.reason)
    .map((k) => c[k].reason);

  const sendLabel = session.runtime_state === "running" ? (session.capabilities.steer_mid_turn ? "Steer" : "Queue follow-up") : "Send";

  return (
    <section className="detail" aria-labelledby="detail-title">
      <div className="detail-head">
        <h2 id="detail-title" tabIndex={-1}>
          <RuntimeIcon runtime={session.runtime} />
          <span className="detail-title">{session.title || `${session.runtime} · ${session.session_id.slice(0, 8)}`}</span>
          <StateBadge session={session} />
        </h2>
        <p className="where">
          <span>
            {session.runtime} · {session.mode === "managed" ? "launched by Robin" : "started outside Robin"}
          </span>
          <span title={session.execution_cwd ?? undefined}>{session.execution_cwd ?? "no folder"}</span>
          {session.branch && (
            <span>
              ⎇ <code>{session.branch}</code>
            </span>
          )}
          {session.skip_permissions && <span className="warn-text">skip permissions</span>}
          {session.continued_from && (
            <button type="button" className="link" onClick={() => onOpenSession?.(session.continued_from!.session_id)}>
              ← continued from {session.continued_from.runtime}
            </button>
          )}
          {session.continued_in.map((l) => (
            <button key={l.session_id} type="button" className="link" onClick={() => onOpenSession?.(l.session_id)}>
              continued in {l.runtime} →
            </button>
          ))}
          {session.last_activity_at && <span>last activity {since(session.last_activity_at, Date.now())} ago</span>}
          {session.mode === "observed" && c.takeover?.enabled && (
            <button type="button" className="link" onClick={() => void archive()}>
              {session.archived ? "Restore" : "Archive"}
            </button>
          )}
        </p>
        <ContinueDialog session={session} onOpenSession={onOpenSession} />
      </div>
      <details className="facts-box">
        <summary>Details</summary>
        <dl className="facts">
          <dt>Folder</dt>
          <dd>{session.execution_cwd ?? "—"}</dd>
          {!session.branch && (
            <>
              <dt>Branch</dt>
              <dd>not a git repository</dd>
            </>
          )}
          <dt>Native ID</dt>
          <dd>{session.native_session_id ?? "not reported yet"}</dd>
          <dt>Evidence</dt>
          <dd>{ago(session.state_evidence_at)}</dd>
          {session.independence && (
            <>
              <dt>Review</dt>
              <dd className={session.independence === "not_isolated" ? "warn-text" : undefined}>
                {session.independence === "enforced"
                  ? "Independent: can't read other agents' findings"
                  : "Not isolated: this runtime can't be kept from Robin's findings"}
              </dd>
            </>
          )}
          {session.skip_permissions && (
            <>
              <dt>Permissions</dt>
              <dd className="warn-text">Prompts skipped (chosen at launch)</dd>
            </>
          )}
          <dt>Mode</dt>
          <dd>{session.mode === "managed" ? "Launched by Robin" : "Observed (read-only)"}</dd>
          {session.mode === "managed" && (
            <>
              <dt>Control</dt>
              <dd>{handedOff ? "Handed off to a terminal" : "Robin"}</dd>
            </>
          )}
        </dl>
      </details>

      <p className="sr-only" role="status">
        {`${session.runtime} ${LABEL[sessionState(session, Date.now()).state]}${stale ? `, ${session.connection_state}` : ""}`}
      </p>

      {session.shared_folder_with.length > 0 && (
        <p className="hint warn" role="alert">
          {session.shared_folder_with.length === 1 ? "Another session" : `${session.shared_folder_with.length} other sessions`}{" "}
          may be editing this folder. Their changes can collide with this one&apos;s.
        </p>
      )}

      <SessionFlow session={session} onChanged={onChanged} />

      <Conversation
        sessionId={session.session_id}
        runtime={session.runtime}
        liveCursor={liveCursor}
        running={sessionState(session, Date.now()).state === "running"}
        events={events}
      />

      {session.mode === "observed" && c.takeover?.enabled && (
        <form
          className="compose"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!text.trim() || sending) return;
            setSending(true);
            await act({ type: "takeover", text }, "Send");
            setSending(false);
            onChanged?.();
          }}
        >
          <label htmlFor="compose" className="sr-only">Message</label>
          <div className="term-input">
            <span className="prompt" aria-hidden="true">&gt;</span>
            <textarea
              id="compose"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={2}
              readOnly={sending}
              placeholder="Message this session…   ↵ send · ⇧↵ new line"
              onKeyDown={enterSends}
            />
          </div>
          <p className="status" role="status" aria-live="polite">
            {status}
          </p>
        </form>
      )}

      {session.mode === "observed" && !c.takeover?.enabled && (
        <div className="readonly-note">
          <p>
            Read-only here: Robin follows this session from its transcript and can&apos;t control it.
            {resume && (
              <>
                {" "}
                Continue it with <code>{resume}</code>.
              </>
            )}
          </p>
          {c.takeover?.reason && <p className="hint">Can&apos;t continue it here: {c.takeover.reason}</p>}
          <button type="button" onClick={() => void archive()}>
            {session.archived ? "Restore" : "Archive"}
          </button>
          <p className="status" role="status" aria-live="polite">
            {status}
          </p>
        </div>
      )}

      {session.mode === "managed" && (
        <div className="terminal">
          {handedOff ? (
            <>
              <p className="hint">
                {resume ? (
                  <>
                    Continue in a terminal with <code>{resume}</code>.
                  </>
                ) : (
                  "This session's native id can't be used in a command."
                )}{" "}
                Close it before taking control back.
              </p>
              <div className="actions">
                <button type="button" onClick={() => void openTerminal()}>
                  Open in Terminal
                </button>
                <button
                  type="button"
                  disabled={!c.reclaim.enabled}
                  title={c.reclaim.reason ?? undefined}
                  onClick={() => void act({ type: "reclaim" }, "Take back control")}
                >
                  Take back control
                </button>
              </div>
            </>
          ) : (
            <div className="actions">
              <button
                type="button"
                disabled={!c.handoff.enabled}
                title={c.handoff.reason ?? "Robin stops its process; you continue the session in a terminal"}
                onClick={() => void handOff()}
              >
                Hand off to terminal
              </button>
            </div>
          )}
        </div>
      )}

      {session.mode === "managed" && (
        <form
          className="compose"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) void act({ type: "message", text }, sendLabel);
          }}
        >
          {c.message.enabled && (
            <>
              <label htmlFor="compose" className="sr-only">Message</label>
              <div className="term-input">
                <span className="prompt" aria-hidden="true">&gt;</span>
                <textarea
                  id="compose"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={2}
                  placeholder={`Message this session…${sendLabel === "Queue follow-up" ? " (a turn is running: this is queued)" : ""}   ↵ ${sendLabel.toLowerCase()} · ⇧↵ new line`}
                  onKeyDown={enterSends}
                />
              </div>
            </>
          )}
          <div className="actions">
            <button
              type="button"
              disabled={!c.interrupt.enabled}
              title={c.interrupt.reason ?? undefined}
              onClick={() => void act({ type: "interrupt" }, "Interrupt")}
            >
              Interrupt
            </button>
            <button type="button" onClick={() => void archive()}>
              {session.archived ? "Restore" : "Archive"}
            </button>
            <button
              type="button"
              className="danger"
              disabled={!c.stop.enabled}
              title={c.stop.reason ?? undefined}
              onClick={() => {
                if (window.confirm(`Stop the ${session.runtime} session in ${session.execution_cwd}? This ends the runtime process.`))
                  void act({ type: "stop" }, "Stop");
              }}
            >
              Stop session
            </button>
          </div>
          {!c.message.enabled && c.message.reason && <p className="hint">Controls unavailable: {c.message.reason}</p>}
          {[...new Set(disabledReasons)].map((r) => (
            <p className="hint" key={r}>
              {r}
            </p>
          ))}
          <p className="status" role="status" aria-live="polite">
            {status}
          </p>
        </form>
      )}
      {session.mode === "managed" && (
        <WorktreePanel
          sessionId={session.session_id}
          connected={session.connection_state === "connected"}
          updatedAt={session.updated_at}
        />
      )}
      {session.mode === "managed" && <Runs sessionId={session.session_id} updatedAt={session.updated_at} />}
    </section>
  );
}
