import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ContinuePreview, type Session } from "../api";

const RUNTIMES = ["claude", "codex", "pi"] as const;
const NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", pi: "Pi" };
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const DROPPED: [string, (n: number) => string][] = [
  ["images", (n) => `${plural(n, "image")} → [image]`],
  ["sidechain_records", (n) => plural(n, "subagent record")],
  ["system_reminders", (n) => plural(n, "system message")],
  ["encrypted_reasoning", (n) => plural(n, "encrypted reasoning block")],
  ["before_compaction", (n) => `${plural(n, "message")} before the last compaction`],
  ["unknown_items", (n) => `${plural(n, "item")} of unknown kinds`],
];

type State =
  | { kind: "idle" }
  | { kind: "previewing"; runtime: string }
  | { kind: "ready"; preview: ContinuePreview; text: string }
  | { kind: "refused"; preview: ContinuePreview }
  | { kind: "confirming"; preview: ContinuePreview }
  | { kind: "failed"; runtime: string; message: string; kept?: string; stale?: boolean };

const START_DEADLINE_MS = 60_000;
const k = (n: number) => `${Math.floor(n / 1000)}k`; // floored, like the server's refusal message

/** "Continue in…": carry this session into another runtime, after a preview of what crosses. */
export function ContinueDialog({ session, onOpenSession }: { session: Session; onOpenSession?: (id: string) => void }) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const busy = session.mode === "managed" && ["running", "waiting_for_input"].includes(session.runtime_state);
  const others = RUNTIMES.filter((r) => r !== session.runtime);
  if (!(RUNTIMES as readonly string[]).includes(session.runtime) || !session.native_session_id) return null;

  async function preview(runtime: string) {
    setState({ kind: "previewing", runtime });
    try {
      const p = await api.continuePreview(session.session_id, runtime);
      setState(p.refusal ? { kind: "refused", preview: p } : { kind: "ready", preview: p, text: p.default_text });
    } catch (e) {
      setState({ kind: "failed", runtime, message: e instanceof ApiError ? e.message : "Preview failed" });
    }
  }

  async function confirm(p: ContinuePreview, text: string) {
    const runtime = p.target.runtime;
    setState({ kind: "confirming", preview: p });
    let kept: string | undefined;
    try {
      const r = await api.continueIn(session.session_id, runtime, text, p.preview_hash ?? "");
      kept = r.session_id;
      let action = r.action;
      const done = () => ["acknowledged", "failed", "cancelled", "outcome_unknown"].includes(action.state);
      const deadline = Date.now() + START_DEADLINE_MS;
      while (!done() && Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 300));
        if (!mounted.current) return;
        action = await api.action(action.action_id);
      }
      if (!mounted.current) return; // the user moved to another session: don't pull them back
      if (!done()) {
        setState({
          kind: "failed",
          runtime,
          kept,
          message: `${NAMES[runtime]} is taking too long to start. The converted session is kept; you can open it from there.`,
        });
        return;
      }
      if (action.state === "acknowledged") {
        setState({ kind: "idle" });
        onOpenSession?.(r.session_id);
        return;
      }
      setState({
        kind: "failed",
        runtime,
        kept,
        message: `${NAMES[runtime]} didn't start${action.error ? ` (${action.error.message})` : ""}. The converted session is kept; you can continue it from there.`,
      });
    } catch (e) {
      if (!mounted.current) return;
      const stale = e instanceof ApiError && e.code === "preview_stale";
      setState({
        kind: "failed",
        runtime,
        kept,
        stale,
        message: stale ? "The session changed since the preview." : e instanceof ApiError ? e.message : "Continue failed",
      });
    }
  }

  return (
    <div className="continue">
      {state.kind === "idle" && (
        <details className="continue-menu">
          <summary
            role="button"
            aria-disabled={busy}
            title={busy ? "A turn is running: wait for it, or interrupt it first" : "Carry this session into another runtime"}
            onClick={(e) => busy && e.preventDefault()}
          >
            Continue in…
          </summary>
          <div className="continue-options">
            {others.map((r) => (
              <button key={r} type="button" className="link" onClick={() => void preview(r)}>
                {NAMES[r]}
              </button>
            ))}
          </div>
        </details>
      )}
      {state.kind === "previewing" && <p className="status" role="status">Reading the session for {NAMES[state.runtime]}…</p>}
      {(state.kind === "ready" || state.kind === "refused" || state.kind === "confirming") && (
        <section className="continue-panel" aria-labelledby="continue-title">
          <h3 id="continue-title">Continue in {NAMES[state.preview.target.runtime]}</h3>
          <Summary p={state.preview} />
          {state.kind === "refused" ? (
            <>
              <p className="hint warn" role="alert">{state.preview.refusal?.message}</p>
              <div className="actions">
                <button type="button" onClick={() => void preview(state.preview.target.runtime)}>
                  Preview again
                </button>
                <button type="button" onClick={() => setState({ kind: "idle" })}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (state.kind === "ready" && state.text.trim()) void confirm(state.preview, state.text);
              }}
            >
              <label htmlFor="continue-text">First message</label>
              <textarea
                id="continue-text"
                rows={2}
                value={state.kind === "ready" ? state.text : ""}
                readOnly={state.kind === "confirming"}
                onChange={(e) => state.kind === "ready" && setState({ ...state, text: e.target.value })}
              />
              <div className="actions">
                <button type="button" disabled={state.kind === "confirming"} onClick={() => setState({ kind: "idle" })}>
                  Cancel
                </button>
                <button type="submit" disabled={state.kind === "confirming" || (state.kind === "ready" && !state.text.trim())}>
                  Continue
                </button>
              </div>
              {state.kind === "confirming" && (
                <p className="status" role="status">
                  Writing the session and starting {NAMES[state.preview.target.runtime]}…
                </p>
              )}
            </form>
          )}
        </section>
      )}
      {state.kind === "failed" && (
        <div className="continue-panel">
          <p className="hint warn" role="alert">
            {state.message}
          </p>
          <div className="actions">
            {state.kept && onOpenSession && (
              <button type="button" onClick={() => onOpenSession(state.kept!)}>
                Open the converted session
              </button>
            )}
            {!state.kept && (
              <button type="button" onClick={() => void preview(state.runtime)}>
                Preview again
              </button>
            )}
            <button type="button" onClick={() => setState({ kind: "idle" })}>
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Summary({ p }: { p: ContinuePreview }) {
  if (!p.messages || !p.estimate || !p.dropped) return null;
  const m = p.messages;
  const d = p.dropped;
  const gone = DROPPED.filter(([key]) => d[key]).map(([key, label]) => label(d[key]));
  const share = Math.min(1, p.estimate.tokens / Math.max(1, p.estimate.limit_tokens));
  return (
    <dl className="facts">
      <dt>Messages</dt>
      <dd>
        {m.total} ({m.user} from you, {m.assistant} {m.assistant === 1 ? "reply" : "replies"}) ·{" "}
        {plural(d.tool_calls_as_text ?? 0, "tool call")}
        {d.thinking_as_text ? ` and ${plural(d.thinking_as_text, "thinking block")}` : ""} as text
      </dd>
      <dt>Dropped</dt>
      <dd>{gone.length ? gone.join(", ") : "nothing"}</dd>
      {p.compaction?.source_summary_used && (
        <>
          <dt>Compaction</dt>
          <dd>Starts from {p.source.runtime}&apos;s latest summary and the messages it kept</dd>
        </>
      )}
      <dt>Size</dt>
      <dd>
        <meter min={0} max={1} high={0.99} value={share} aria-label="Share of the allowed size" /> ~{k(p.estimate.tokens)} of{" "}
        {k(p.estimate.limit_tokens)} allowed
      </dd>
      {p.inherits && (p.inherits.task_id || p.inherits.skip_permissions || p.inherits.independence) && (
        <>
          <dt>Keeps</dt>
          <dd>
            {[
              p.inherits.task_id && "its task",
              p.inherits.skip_permissions && "permission prompts skipped",
              p.inherits.independence && "independent review",
            ]
              .filter(Boolean)
              .join(" · ")}
          </dd>
        </>
      )}
      {p.warnings.map((w) => (
        <dd key={w} className="warn-text">
          {w}
        </dd>
      ))}
    </dl>
  );
}
