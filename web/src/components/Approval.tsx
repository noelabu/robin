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

const OTHER = "\u0000other";

/** Answer the questions here: an option or your own text for each, then Submit. Decline tells the agent you
 * won't answer; Answer in terminal (a terminal session waiting on Robin) hands the question back to it. */
export function QuestionForm({ item, onAnswered }: { item: AttentionItem; onAnswered?: () => void }) {
  const qs = item.detail.questions ?? [];
  const [picks, setPicks] = useState<string[][]>(() => qs.map(() => []));
  const [other, setOther] = useState<string[]>(() => qs.map(() => ""));
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const answerFor = (n: number): string | string[] | null => {
    const q = qs[n];
    const text = other[n].trim();
    const chosen = picks[n].flatMap((p) => (p === OTHER ? (text ? [text] : []) : [p]));
    if (!chosen.length) return null;
    return q.multi ? chosen : chosen[0];
  };
  const answers = qs.map((_, n) => answerFor(n));
  const complete = answers.every((a) => a !== null);

  function pick(n: number, label: string, on: boolean) {
    setPicks((all) =>
      all.map((p, i) => (i !== n ? p : qs[n].multi ? (on ? [...p, label] : p.filter((x) => x !== label)) : [label])),
    );
  }

  async function send(decision: "answer" | "deny" | "terminal", label: string) {
    setBusy(true);
    setStatus(`${label}…`);
    try {
      const body =
        decision === "answer"
          ? Object.fromEntries(qs.map((q, n) => [q.question, answers[n] as string | string[]]))
          : undefined;
      await api.respond(item.attention_id, decision, body);
      setStatus(`${label}: sent`);
      onAnswered?.();
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label} failed: ${e.message}` : `${label} failed`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="question-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete) void send("answer", "Submit");
      }}
    >
      {qs.map((q, n) => (
        <fieldset key={n} className="question">
          <legend className={n > 0 ? undefined : "sr-only"}>{q.question}</legend>
          {[...q.options.map((o) => o.label), OTHER].map((label) => {
            const o = q.options.find((x) => x.label === label);
            return (
              <label key={label} className="inline check question-choice">
                <input
                  type={q.multi ? "checkbox" : "radio"}
                  name={`${item.attention_id}-${n}`}
                  checked={picks[n].includes(label)}
                  onChange={(e) => pick(n, label, e.target.checked)}
                />{" "}
                {label === OTHER ? (
                  <>
                    Other:{" "}
                    <input
                      type="text"
                      aria-label={`Your own answer to ${q.question}`}
                      value={other[n]}
                      onFocus={() => !picks[n].includes(OTHER) && pick(n, OTHER, true)}
                      onChange={(e) => setOther((all) => all.map((t, i) => (i === n ? e.target.value : t)))}
                    />
                  </>
                ) : (
                  <>
                    {label}
                    {o?.description && <span className="muted"> — {o.description}</span>}
                  </>
                )}
              </label>
            );
          })}
        </fieldset>
      ))}
      {item.detail.answer_in === "hook" && (
        <p className="muted question-where">
          Answer here.
          {item.detail.until
            ? ` The terminal asks at ${new Date(item.detail.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}, or choose Answer in terminal.`
            : " Or choose Answer in terminal."}
        </p>
      )}
      {item.detail.answer_in === "robin" && (
        <p className="muted question-where">
          Or answer it with <code>robin chat {item.session_id}</code> or <code>robin answer {item.attention_id}</code>.
        </p>
      )}
      <div className="actions">
        <button className="primary" type="submit" disabled={!complete || busy}>
          Submit
        </button>
        <button type="button" disabled={busy} onClick={() => void send("deny", "Decline")}>
          Decline
        </button>
        {item.allowed_responses.includes("terminal") && (
          <button type="button" disabled={busy} onClick={() => void send("terminal", "Answer in terminal")}>
            Answer in terminal
          </button>
        )}
      </div>
      {status && (
        <p className="status" role="status">
          {status}
        </p>
      )}
    </form>
  );
}

/** The questions an agent asked: a form when they can be answered here, otherwise their options. */
export function QuestionList({ item, onAnswered }: { item: AttentionItem; onAnswered?: () => void }) {
  if (item.allowed_responses.includes("answer")) return <QuestionForm item={item} onAnswered={onAnswered} />;
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
export function QuestionBox({ item, onAnswered }: { item: AttentionItem; onAnswered?: () => void }) {
  return (
    <div className="approval-box question-box">
      <div className="approval-head">
        <span className="kind">Needs an answer</span>
        <strong>{item.title}</strong>
      </div>
      <QuestionList item={item} onAnswered={onAnswered} />
    </div>
  );
}
