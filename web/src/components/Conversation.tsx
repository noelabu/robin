import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type ConversationMessage } from "../api";
import { useTick } from "../map/useMap";
import { Prose } from "./Prose";

const REFRESH_MS = 600; // new events arrive in bursts; refetch once they settle
const NEAR_BOTTOM = 80; // px: closer than this, a new message keeps the view pinned to the bottom

const FOLD_LINES = 18; // a longer message shows its start, with Show more

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** One session as a chat: prompts and full replies, tool calls folded between them; the raw events are a tab away. */
export function Conversation(props: {
  sessionId: string;
  runtime: string;
  liveCursor: number | undefined; // the session's newest event: a change means there may be more to show
  running?: boolean; // the session is working now: the chat ends with a live line
  events: ReactNode;
}) {
  const [tab, setTab] = useState<"chat" | "events">("chat");
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null);
  const [error, setError] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    let live = true;
    const t = window.setTimeout(
      () =>
        void api
          .conversation(props.sessionId)
          .then((r) => {
            if (!live) return;
            setMessages(r.messages);
            setError(false);
          })
          .catch(() => live && setError(true)),
      messages === null ? 0 : REFRESH_MS,
    );
    return () => {
      live = false;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.sessionId, props.liveCursor]);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [messages, tab]);

  const who = props.runtime.charAt(0).toUpperCase() + props.runtime.slice(1);
  return (
    <section className="chat" aria-label="Conversation">
      <div className="chat-head">
        <h3>Conversation</h3>
        <div className="chat-tabs" role="tablist" aria-label="Show">
          <button role="tab" aria-selected={tab === "chat"} onClick={() => setTab("chat")}>
            Conversation
          </button>
          <button role="tab" aria-selected={tab === "events"} onClick={() => setTab("events")}>
            Events
          </button>
        </div>
      </div>
      {tab === "events" ? (
        props.events
      ) : (
        <div
          className="chat-scroll"
          ref={box}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM;
          }}
        >
          {messages === null && !error && <p className="empty">Loading…</p>}
          {error && (
            <p className="empty">
              Couldn&apos;t load the conversation. If Robin was just updated, restart it (<code>make run</code>).
            </p>
          )}
          {messages?.length === 0 && <p className="empty">No messages yet.</p>}
          <ol className="chat-list">
            {turns(messages ?? []).map((t, i, all) => (
              <Turn key={i} turn={t} who={who} live={i === all.length - 1} />
            ))}
            {props.running && messages && <Live turn={turns(messages).at(-1)} />}
          </ol>
        </div>
      )}
    </section>
  );
}

/** A long message shows its first lines until asked for the rest, so one report can't bury the chat. */
function Folded({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > FOLD_LINES || text.length > FOLD_LINES * 90;
  return (
    <>
      <div className={long && !open ? "folded" : undefined}>
        <Prose text={text} />
      </div>
      {long && (
        <button className="link more-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </>
  );
}

type Said = Extract<ConversationMessage, { role: "user" | "assistant" }>;
type TurnT = { prompt: Said | null; steps: ConversationMessage[]; answer: Said | null };

/** Prompt → the work in between (notes and tool calls) → the reply the turn ended with. */
function turns(messages: ConversationMessage[]): TurnT[] {
  const out: TurnT[] = [];
  for (const m of messages) {
    if (m.role === "user" || !out.length) out.push({ prompt: null, steps: [], answer: null });
    const t = out[out.length - 1];
    if (m.role === "user") t.prompt = m;
    else t.steps.push(m);
  }
  for (const t of out) {
    const last = t.steps[t.steps.length - 1];
    if (last?.role === "assistant") t.answer = t.steps.pop() as Said;
  }
  return out;
}

function Turn({ turn, who, live }: { turn: TurnT; who: string; live: boolean }) {
  const notes = turn.steps.filter((m) => m.role === "assistant").length;
  const calls = turn.steps.reduce((n, m) => n + (m.role === "tools" ? m.items.length : 0), 0);
  const working = live && !turn.answer && turn.steps.length > 0;
  return (
    <>
      {turn.prompt && <Line m={turn.prompt} who={who} />}
      {turn.steps.length > 0 && (
        <li className="msg work">
          <details>
            <summary>
              {working ? "So far" : "Worked"}
              {notes > 0 && ` · ${notes} note${notes === 1 ? "" : "s"}`}
              {calls > 0 && ` · ${calls} tool call${calls === 1 ? "" : "s"}`}
            </summary>
            <ol className="steps">
              {turn.steps.flatMap((m, k) =>
                m.role === "tools"
                  ? m.items.map((t, j) => (
                      <li key={`${k}-${j}`} className="step call">
                        {t}
                      </li>
                    ))
                  : [
                      <li key={k} className="step note">
                        <Prose text={m.text} />
                      </li>,
                    ],
              )}
            </ol>
          </details>
        </li>
      )}
      {turn.answer && <Line m={turn.answer} who={who} />}
    </>
  );
}

function Line({ m, who }: { m: Said; who: string }) {
  return (
    <li className={`msg ${m.role}`}>
      <span className="sigil" aria-hidden="true">{m.role === "user" ? ">" : "⏺"}</span>
      <div className="body">
        <span className="sr-only">{m.role === "user" ? "You" : who}: </span>
        <Folded text={m.text} />
      </div>
      <time dateTime={m.at}>{clock(m.at)}</time>
    </li>
  );
}

const SPIN = ["✻", "✽", "✶", "✳", "✢", "·"];

/** The turn in progress, as a terminal shows it: a live mark, how long it has run, and the step it's on. */
function Live({ turn }: { turn: TurnT | undefined }) {
  const now = useTick(1000);
  const started = turn?.prompt?.at ?? turn?.steps[0]?.at;
  const secs = started ? Math.max(0, Math.round((now - Date.parse(started)) / 1000)) : null;
  const elapsed = secs === null ? null : secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
  const last = turn?.answer ? null : turn?.steps.at(-1);
  const step = last ? (last.role === "tools" ? last.items[last.items.length - 1] : last.text.split("\n")[0]) : null;
  return (
    <li className="msg live" role="status" aria-live="polite">
      <span className="spin" aria-hidden="true">
        {SPIN[Math.floor(now / 250) % SPIN.length]}
      </span>
      <div className="body">
        <span className="what">Working…</span>
        {elapsed && <span className="elapsed"> ({elapsed})</span>}
        {step && <span className="step">⎿ {step}</span>}
      </div>
    </li>
  );
}
