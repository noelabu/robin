import { useState } from "react";
import { api, ApiError, type Session } from "../api";
import { StateBadge } from "./StateBadge";

function ago(iso: string | null): string {
  if (!iso) return "no evidence yet";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
}

export function SessionDetail({ session }: { session: Session }) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const c = session.controls;

  async function act(body: { type: string; text?: string }, label: string) {
    setStatus(`${label}…`);
    try {
      const { action } = await api.act(session.session_id, body);
      setStatus(`${label}: ${action.state}`);
      if (body.type === "message") setText("");
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} not sent: ${e.message}` : `${label} failed`);
    }
  }

  const sendLabel = session.runtime_state === "running" ? (session.capabilities.steer_mid_turn ? "Steer" : "Queue follow-up") : "Send";

  return (
    <section className="detail" aria-labelledby="detail-title">
      <div className="detail-head">
        <h2 id="detail-title">
          {session.runtime} <StateBadge session={session} />
        </h2>
        <dl className="facts">
          <dt>Folder</dt>
          <dd>{session.execution_cwd ?? "—"}</dd>
          <dt>Native ID</dt>
          <dd>{session.native_session_id ?? "not reported yet"}</dd>
          <dt>Evidence</dt>
          <dd>{ago(session.state_evidence_at)}</dd>
          <dt>Mode</dt>
          <dd>{session.mode === "managed" ? "Launched by Robin" : "Observed (read-only)"}</dd>
        </dl>
      </div>

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
        {sendLabel === "Queue follow-up" && (
          <p className="hint">{session.runtime} applies input sent during a turn after the turn ends.</p>
        )}
        <p className="status" role="status" aria-live="polite">
          {status}
        </p>
      </form>
    </section>
  );
}
