import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type AttentionItem } from "../api";

export function describe(item: AttentionItem): string {
  const input = item.detail.input;
  if (input && typeof input === "object" && "command" in (input as Record<string, unknown>))
    return String((input as Record<string, unknown>).command);
  if (input !== undefined) return JSON.stringify(input);
  return item.detail.reason ? String(item.detail.reason) : "";
}

/** Open permission requests by session, reloaded whenever `refreshKey` changes. */
export function useApprovals(refreshKey: unknown): { bySession: Map<string, AttentionItem[]>; reload: () => void } {
  const [items, setItems] = useState<AttentionItem[]>([]);
  const reload = useCallback(() => {
    void api
      .attention()
      .then((r) => setItems(r.items ?? []))
      .catch(() => undefined);
  }, []);
  useEffect(reload, [reload, refreshKey]);
  const bySession = new Map<string, AttentionItem[]>();
  for (const i of items)
    if (i.kind === "approval" && i.allowed_responses.includes("allow"))
      bySession.set(i.session_id, [...(bySession.get(i.session_id) ?? []), i]);
  return { bySession, reload };
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
