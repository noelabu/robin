import { useState } from "react";
import { api, ApiError, type Session } from "../api";
import { Runs } from "./Runs";
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

export function SessionDetail({ session, onChanged }: { session: Session; onChanged?: () => void }) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const c = session.controls;

  async function act(body: { type: string; text?: string; interrupt_running?: boolean }, label: string) {
    setStatus(`${label}…`);
    try {
      const { action } = await api.act(session.session_id, body);
      setStatus(`${label}: ${action.state}`);
      if (body.type === "message") setText("");
      return action;
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} not sent: ${e.message}` : `${label} failed`);
      return null;
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
  const disabledReasons = (["interrupt", "stop", "handoff", "reclaim"] as const)
    .filter((k) => !c[k].enabled && c[k].reason && c[k].reason !== c.message.reason)
    .map((k) => c[k].reason);

  const sendLabel = session.runtime_state === "running" ? (session.capabilities.steer_mid_turn ? "Steer" : "Queue follow-up") : "Send";

  return (
    <section className="detail" aria-labelledby="detail-title">
      <div className="detail-head">
        <h2 id="detail-title" tabIndex={-1}>
          {session.runtime} <StateBadge session={session} />
        </h2>
        <dl className="facts">
          <dt>Folder</dt>
          <dd>{session.execution_cwd ?? "—"}</dd>
          <dt>Branch</dt>
          <dd>{session.branch ?? "not a git repository"}</dd>
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
          <dt>Mode</dt>
          <dd>{session.mode === "managed" ? "Launched by Robin" : "Observed (read-only)"}</dd>
          {session.mode === "managed" && (
            <>
              <dt>Control</dt>
              <dd>{handedOff ? "Handed off to a terminal" : "Robin"}</dd>
            </>
          )}
        </dl>
      </div>

      <p className="sr-only" role="status">
        {`${session.runtime} ${LABEL[session.runtime_state]}${stale ? `, ${session.connection_state}` : ""}`}
      </p>

      {session.shared_folder_with.length > 0 && (
        <p className="hint warn" role="alert">
          {session.shared_folder_with.length === 1 ? "Another session" : `${session.shared_folder_with.length} other sessions`}{" "}
          may be editing this folder. Their changes can collide with this one&apos;s.
        </p>
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

      <form
        className="compose"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) void act({ type: "message", text }, sendLabel);
        }}
      >
        <label htmlFor="compose">Message</label>
        <textarea
          id="compose"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!c.message.enabled}
          rows={3}
          placeholder={c.message.enabled ? "Instruction for this session" : (c.message.reason ?? "")}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit();
          }}
        />
        <div className="actions">
          <button type="submit" className="primary" disabled={!c.message.enabled || !text.trim()}>
            {sendLabel}
          </button>
          <button
            type="button"
            disabled={!c.interrupt.enabled}
            title={c.interrupt.reason ?? undefined}
            onClick={() => void act({ type: "interrupt" }, "Interrupt")}
          >
            Interrupt
          </button>
          <button
            type="button"
            onClick={async () => {
              try {
                await api.archiveSession(session.session_id, !session.archived);
                setStatus(session.archived ? "Restored" : "Archived: hidden from default views, nothing deleted");
                onChanged?.();
              } catch (e) {
                setStatus(e instanceof ApiError ? `Archive failed: ${e.message}` : "Archive failed");
              }
            }}
          >
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
        {sendLabel === "Queue follow-up" && (
          <p className="hint">{session.runtime} applies input sent during a turn after the turn ends.</p>
        )}
        <p className="status" role="status" aria-live="polite">
          {status}
        </p>
      </form>
      {session.mode === "managed" && <Runs sessionId={session.session_id} updatedAt={session.updated_at} />}
    </section>
  );
}
