import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type AttentionItem } from "../api";

export function describe(item: AttentionItem): string {
  const input = item.detail.input;
  if (input && typeof input === "object" && "command" in (input as Record<string, unknown>))
    return String((input as Record<string, unknown>).command);
  if (input !== undefined) return JSON.stringify(input);
  return item.detail.reason ? String(item.detail.reason) : "";
}

/** Open permission requests and questions by session, reloaded whenever `refreshKey` changes. */
export function useApprovals(refreshKey: unknown): {
  bySession: Map<string, AttentionItem[]>;
  questions: Map<string, AttentionItem[]>;
  reload: () => void;
} {
  const [items, setItems] = useState<AttentionItem[]>([]);
  const reload = useCallback(() => {
    void api
      .attention()
      .then((r) => setItems(r.items ?? []))
      .catch(() => undefined);
  }, []);
  useEffect(reload, [reload, refreshKey]);
  const bySession = new Map<string, AttentionItem[]>();
  const questions = new Map<string, AttentionItem[]>();
  for (const i of items)
    if (i.kind === "approval" && i.allowed_responses.includes("allow"))
      bySession.set(i.session_id, [...(bySession.get(i.session_id) ?? []), i]);
    else if (i.kind === "question") questions.set(i.session_id, [...(questions.get(i.session_id) ?? []), i]);
  return { bySession, questions, reload };
}

/** One permission request with its answer buttons. Approve and Deny answer the agent. */
export function ApprovalBox({ item, onAnswered }: { item: AttentionItem; onAnswered?: () => void }) {
  const [status, setStatus] = useState<string | null>(null);
  async function answer(decision: "allow" | "deny") {
    const label = decision === "allow" ? "Approve" : "Deny";
    setStatus(`${label}…`);
    try {
      await api.respond(item.attention_id, decision);
      setStatus(`${label}: sent`);
      onAnswered?.();
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} failed: ${e.message}` : `${label} failed`);
    }
  }
  return (
    <div className="approval-box">
      <div className="approval-head">
        <span className="kind">Needs permission</span>
        <strong>{item.title}</strong>
      </div>
      {describe(item) && <code className="attention-detail">{describe(item)}</code>}
      <div className="actions">
        <button className="primary" onClick={() => void answer("allow")}>
          Approve
        </button>
        <button onClick={() => void answer("deny")}>Deny</button>
      </div>
      {status && (
        <p className="status" role="status">
          {status}
        </p>
      )}
    </div>
  );
}

/** The questions an agent asked and their options. Answered where the session runs, not here. */
export function QuestionList({ item }: { item: AttentionItem }) {
  const qs = item.detail.questions ?? [];
  return (
    <>
      {qs.map((q, n) => (
        <div key={n} className="question">
          {n > 0 && <strong>{q.question}</strong>}
          <ol className="question-options">
            {q.options.map((o) => (
              <li key={o.label}>
                {o.label}
                {o.description && <span className="muted"> — {o.description}</span>}
              </li>
            ))}
          </ol>
        </div>
      ))}
      {item.detail.answer_in === "terminal" && <p className="muted question-where">Answer it in the session&apos;s terminal.</p>}
    </>
  );
}

/** A question waiting on the user, shown at the top of its card's panel. */
export function QuestionBox({ item }: { item: AttentionItem }) {
  return (
    <div className="approval-box question-box">
      <div className="approval-head">
        <span className="kind">Needs an answer</span>
        <strong>{item.title}</strong>
      </div>
      <QuestionList item={item} />
    </div>
  );
}
