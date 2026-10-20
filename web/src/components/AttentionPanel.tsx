import { useEffect, useState } from "react";
import { api, ApiError, type AttentionItem } from "../api";
import { describe, QuestionList } from "./Approval";

/** Questions, approvals and failures. Only Approve/Deny and a question's form answer the agent; Dismiss only
 * hides the notice. */
export function AttentionPanel(props: { refreshKey: number; onOpenSession: (id: string) => void }) {
  const [items, setItems] = useState<AttentionItem[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  const load = () => void api.attention().then((r) => setItems(r.items)).catch(() => undefined);
  useEffect(load, [props.refreshKey]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setStatus(`${label}…`);
    try {
      await fn();
      setStatus(`${label}: done`);
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} failed: ${e.message}` : `${label} failed`);
    }
    load();
  }

  return (
    <section className="attention" aria-labelledby="attention-title">
      <h2 id="attention-title">
        Needs attention <span className="count" role="status">
          {items.length}
        </span>
      </h2>
      {!items.length ? (
        <p className="empty">Nothing is waiting on you.</p>
      ) : (
        <ul className="attention-list">
          {items.map((i) => (
            <li key={i.attention_id} className={`attention-item kind-${i.kind}`}>
              <div className="attention-head">
                <span className="kind">{i.kind}</span>
                <strong>{i.title}</strong>
                {i.state === "acknowledged" && <span className="muted"> · acknowledged</span>}
              </div>
              {i.detail.questions ? (
                <QuestionList item={i} onAnswered={load} />
              ) : (
                describe(i) && <code className="attention-detail">{describe(i)}</code>
              )}
              <div className="actions">
                {i.allowed_responses.includes("allow") && (
                  <>
                    <button className="primary" onClick={() => void run("Approve", () => api.respond(i.attention_id, "allow"))}>
                      Approve
                    </button>
                    <button onClick={() => void run("Deny", () => api.respond(i.attention_id, "deny"))}>Deny</button>
                  </>
                )}
                {i.kind === "failure" && i.state === "open" && (
                  <button onClick={() => void run("Acknowledge", () => api.notice(i.attention_id, "acknowledge"))}>
                    Acknowledge
                  </button>
                )}
                <button
                  title={i.kind === "approval" ? "Hides this notice; the agent keeps waiting for an answer" : undefined}
                  onClick={() => void run("Dismiss", () => api.notice(i.attention_id, "dismiss"))}
                >
                  Dismiss
                </button>
                <button onClick={() => props.onOpenSession(i.session_id)}>Open session</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
    </section>
  );
}
