import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type AttentionItem,
  type FileTouch,
  type JobStatus,
  type MapCard,
  type MapState,
  type Session,
  type SessionActivity,
} from "../api";
import { ApprovalBox, QuestionBox } from "../components/Approval";
import { Changes } from "../components/Changes";
import { ago } from "./time";

const SHOWN_FILES = 12;

function FileList({ files, now, hot }: { files: FileTouch[]; now: number; hot: boolean }) {
  return (
    <ul className="tp-files">
      {files.slice(0, SHOWN_FILES).map((f, i) => (
        <li key={f.path} className={hot && i === 0 ? "hot" : undefined}>
          <span title={f.path}>
            {f.path}
            {f.n && f.n > 1 ? ` ×${f.n}` : ""}
          </span>
          <span>{ago(f.at, now)}</span>
        </li>
      ))}
      {files.length > SHOWN_FILES && <li className="none">and {files.length - SHOWN_FILES} more</li>}
    </ul>
  );
}

const STATUS: Record<JobStatus, [string, string]> = {
  completed: ["Completed", "ok"],
  failed: ["Failed", "bad"],
  killed: ["Killed", ""],
};

/** How a job or agent is doing, in words: how it ended, else working (while its parent runs) or not started. */
function statusText(status: JobStatus | null | undefined, running: boolean): [string, string] {
  if (status) return STATUS[status];
  return running ? ["Working", "run"] : ["No end reported", ""];
}

/** A workflow opened: its agents by phase, each with how it ended. */
function WorkflowAgents({ card, detail, running }: { card: MapCard; detail: SessionActivity; running: boolean }) {
  const agents = Object.values((card.run && detail.runs?.[card.run]) || {});
  if (!agents.length) return <p className="tp-none tp-pad">No agents have started yet</p>;
  const phases = new Map<string, typeof agents>();
  for (const a of agents) phases.set(a.phase ?? "", [...(phases.get(a.phase ?? "") ?? []), a]);
  return (
    <>
      {[...phases].map(([name, list]) => {
        const done = list.filter((a) => a.status === "completed").length;
        const failed = list.filter((a) => a.status === "failed").length;
        return (
          <div key={name}>
            <div className="tp-phase">
              <span>{name || "Agents"}</span>
              <span>
                {done}/{list.length}
                {failed > 0 && ` · ${failed} failed`}
              </span>
            </div>
            <ul className="tp-agents">
              {list.map((a, i) => {
                const [text, cls] = statusText(a.status, running);
                return (
                  <li key={`${a.label}-${i}`}>
                    <span>{a.label ?? "agent"}</span>
                    <span className={cls}>{text}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </>
  );
}

/** A skill or background shell opened: what it was asked to do and how it ended. */
function JobFacts({ card, state, now }: { card: MapCard; state: MapState; now: number }) {
  const [text] = statusText(card.status, state === "running");
  const when = card.ended_at && card.status ? `ended ${ago(card.ended_at, now)} ago` : card.started_at ? `started ${ago(card.started_at, now)} ago` : "";
  return (
    <dl className="tp-kv">
      {card.kind === "shell" && card.target && (
        <>
          <dt>Command</dt>
          <dd>{card.target}</dd>
        </>
      )}
      {card.kind === "skill" && card.args && (
        <>
          <dt>Args</dt>
          <dd>{card.args}</dd>
        </>
      )}
      {card.kind === "skill" && !!card.inner?.length && (
        <>
          <dt>Called</dt>
          <dd>{card.inner.join(" → ")}</dd>
        </>
      )}
      <dt>Status</dt>
      <dd>
        {state === "failed" && !card.status ? "Failed" : text}
        {when && ` · ${when}`}
      </dd>
      {card.summary && (
        <>
          <dt>Result</dt>
          <dd>{card.summary}</dd>
        </>
      )}
    </dl>
  );
}

/** A session opened: its skills, workflows and background shells, the finished ones too. */
function BackgroundWork({ detail, sessionId, now }: { detail: SessionActivity; sessionId: string; now: number }) {
  const jobs = (detail.jobs ?? []).filter((j) => j.parent === sessionId);
  if (!jobs.length) return null;
  return (
    <div className="tp-plain">
      <h4>
        <span>Skills, workflows &amp; shells</span>
        <span>{jobs.length}</span>
      </h4>
      <ul className="tp-files">
        {jobs.slice(0, SHOWN_FILES).map((j) => (
          <li key={j.id} className={j.state === "running" ? "hot" : undefined}>
            <span title={j.target ?? undefined}>
              {j.title ?? j.label} · {j.status ? STATUS[j.status][0].toLowerCase() : j.state === "running" ? "working" : j.state}
            </span>
            <span>{ago(j.ended_at ?? j.started_at, now)}</span>
          </li>
        ))}
        {jobs.length > SHOWN_FILES && <li className="none">and {jobs.length - SHOWN_FILES} more</li>}
      </ul>
    </div>
  );
}

/**
 * A card's tasks, opened right under it on the canvas: each task with its status, and the files edited
 * while it was in progress. Sessions without a task list show their changed files instead.
 */
export function TaskPanel(props: {
  card: MapCard;
  state: MapState;
  session?: Session;
  now: number;
  onHeight: (h: number) => void;
  onOpenSession?: (sessionId: string) => void;
  onChanged?: () => void;
  onClose: () => void;
  actions?: boolean; // the Sessions tab has its own controls
  approvals?: AttentionItem[]; // permission requests waiting on the user, shown first
  questions?: AttentionItem[]; // questions the agent asked, answered in its terminal
  onAnswered?: () => void;
  compact?: boolean; // phone: a diff opens full screen
}) {
  const { card, now } = props;
  const [detail, setDetail] = useState<SessionActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string> | null>(null); // null: the task in progress starts open
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    api
      .sessionActivity(card.session_id)
      .then((d) => live && (setDetail(d), setError(null)))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [card.session_id, card.last_activity_at]);

  // The canvas makes room below the card for whatever height this panel ends up.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const report = () => props.onHeight(el.offsetHeight);
    report();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(report) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const kind = card.kind ?? (card.parent ? "subagent" : "session");
  const job = kind === "skill" || kind === "workflow" || kind === "shell";
  const laneId = card.parent ? card.id.slice(card.session_id.length + 1) : "";
  const lane = detail?.lanes.find((l) => l.lane === laneId);
  const todos = lane?.todos ?? [];
  const expanded = open ?? new Set(todos.filter((t) => t.status === "in_progress").map((t) => t.id));
  const toggle = (id: string) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };
  const s = props.session;
  // Finished: nothing running or waiting, and every task (if it kept a list) checked off.
  const done =
    !!detail && !job && (props.state === "idle" || props.state === "stopped") && todos.every((t) => t.status === "completed");
  const doneAt = lane?.last_activity_at ?? card.last_activity_at;

  async function stop() {
    if (!s || !window.confirm(`Stop the ${s.runtime} session in ${s.execution_cwd}? This ends the runtime process.`)) return;
    setStatus("Stopping…");
    try {
      const { action } = await api.act(s.session_id, { type: "stop" });
      setStatus(`Stop: ${action.state}`);
      props.onChanged?.();
    } catch (e) {
      setStatus(e instanceof ApiError ? `Stop not sent: ${e.message}` : "Stop failed");
    }
  }

  async function archive() {
    const archived = !!s?.archived;
    try {
      await api.archiveSession(card.session_id, !archived);
      props.onChanged?.();
      if (!archived) props.onClose(); // archived sessions leave the map
      else setStatus("Restored");
    } catch (e) {
      setStatus(e instanceof ApiError ? `Archive failed: ${e.message}` : "Archive failed");
    }
  }

  return (
    <div
      className="task-panel"
      ref={box}
      role="region"
      aria-label={`Tasks: ${card.title ?? card.runtime}`}
      onPointerDown={(e) => e.stopPropagation()}
      onPointerUp={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape") props.onClose();
      }}
    >
      {props.approvals?.map((a) => (
        <ApprovalBox key={a.attention_id} item={a} onAnswered={props.onAnswered} />
      ))}
      {props.questions?.map((q) => <QuestionBox key={q.attention_id} item={q} onAnswered={props.onAnswered} />)}
      {done && (
        <p className="tp-done" role="status">
          <span className="map-tick" aria-hidden="true">
            <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="#fff" strokeWidth="1.8">
              <path d="M2 5.2 4.2 7.4 8 3" />
            </svg>
          </span>
          <span>Completed</span>
          {doneAt && <span>{ago(doneAt, now)} ago</span>}
        </p>
      )}
      {error && <p className="tp-note">Couldn&apos;t load this session&apos;s activity: {error}</p>}
      {!card.detail ? (
        <p className="tp-note">Robin doesn&apos;t read tasks or file edits for {card.runtime} yet.</p>
      ) : !detail && !error ? (
        <p className="tp-note">Loading…</p>
      ) : kind === "workflow" ? (
        detail && <WorkflowAgents card={card} detail={detail} running={props.state === "running"} />
      ) : job ? (
        <JobFacts card={card} state={props.state} now={now} />
      ) : todos.length ? (
        <ul className="tp-tasks">
          {todos.map((t, i) => {
            const isOpen = expanded.has(t.id);
            const pill =
              t.status === "completed" ? "Completed" : t.status === "in_progress" ? (props.state === "running" ? "Editing now" : "In progress") : "Pending";
            return (
              <li key={t.id} className={`tp-task ${t.status}`}>
                <button aria-expanded={isOpen} onClick={() => toggle(t.id)}>
                  <span className="ic" aria-hidden="true">
                    {t.status === "completed" ? (
                      <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="#fff" strokeWidth="1.8">
                        <path d="M2 5.2 4.2 7.4 8 3" />
                      </svg>
                    ) : (
                      i + 1
                    )}
                  </span>
                  <span className="tx">{t.text}</span>
                  <span className="pill">{pill}</span>
                  <span className="chev" aria-hidden="true">
                    ⌄
                  </span>
                </button>
                {isOpen &&
                  (t.files?.length ? (
                    <FileList files={t.files} now={now} hot={t.status === "in_progress" && props.state === "running"} />
                  ) : (
                    <p className="tp-none">{t.status === "pending" ? "Not started" : "No file edits"}</p>
                  ))}
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="tp-plain">
          <h4>
            <span>Files changed</span>
            <span>{lane?.files_count || ""}</span>
          </h4>
          {lane?.files.length ? (
            <FileList files={lane.files} now={now} hot={props.state === "running"} />
          ) : (
            <p className="tp-none">{lane?.current ? `Last: ${lane.current.tool} ${lane.current.target ?? ""}` : "No file edits"}</p>
          )}
        </div>
      )}
      {kind === "session" && !card.parent && (
        <Changes sessionId={card.session_id} updatedAt={card.last_activity_at ?? ""} compact={props.compact} />
      )}
      {detail && kind === "session" && <BackgroundWork detail={detail} sessionId={card.session_id} now={now} />}
      {props.actions !== false && !card.parent && (props.onOpenSession || s) && (
        <div className="tp-acts">
          {props.onOpenSession && (
            <button className="primary" onClick={() => props.onOpenSession!(card.session_id)}>
              Open in Sessions
            </button>
          )}
          {s?.controls.stop.enabled && (
            <button className="danger" onClick={() => void stop()}>
              Stop
            </button>
          )}
          <button onClick={() => void archive()}>{s?.archived ? "Restore" : "Archive"}</button>
        </div>
      )}
      {status && (
        <p className="tp-note" role="status">
          {status}
        </p>
      )}
    </div>
  );
}
