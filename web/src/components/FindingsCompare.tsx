import { useEffect, useState } from "react";
import { api, ApiError, type Artifact, type Evidence, type Finding, type Packet, type Session } from "../api";

const SYNTHESIS =
  "Synthesize these findings. Say where they agree, where they disagree and why, and which claims are " +
  "unsupported. Don't count repeated claims as independent confirmation. Mark any new claim you add and give " +
  "its evidence.";

function who(f: Finding): string {
  if (f.author.kind === "user") return "you";
  return `${f.author.runtime} ${String(f.author.native_session_id ?? "").slice(0, 8)}`;
}

function evidenceLabel(e: Evidence): string {
  return `${e.path ?? e.artifact_id}${e.range ? `:${e.range}` : ""}`;
}

/** A task's findings side by side; pick claims, open their evidence, and share them with a session. */
export function FindingsCompare(props: {
  taskId: string;
  projectId: string | undefined;
  sessions: Session[];
  refreshKey: number;
}) {
  const [findings, setFindings] = useState<Finding[]>([]);
  const [syntheses, setSyntheses] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<Record<string, Set<number>>>({});
  const [source, setSource] = useState<{ label: string; lines: string[]; first: number; note?: string } | null>(null);
  const [sharing, setSharing] = useState(false);

  useEffect(() => {
    void api
      .findings(props.taskId)
      .then((r) => Promise.all(r.findings.map((f) => api.finding(f.finding_id))))
      .then(setFindings)
      .catch(() => setFindings([]));
    void api
      .artifacts(props.taskId, "synthesis")
      .then((r) => Promise.all(r.artifacts.map((a) => api.artifact(a.artifact_id))))
      .then(setSyntheses)
      .catch(() => setSyntheses([]));
  }, [props.taskId, props.refreshKey]);

  const toggle = (fid: string, n: number) =>
    setSelected((s) => {
      const next = new Set(s[fid] ?? []);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return { ...s, [fid]: next };
    });
  const toggleAll = (f: Finding) =>
    setSelected((s) => {
      const all = (s[f.finding_id]?.size ?? 0) === f.claims.length;
      return { ...s, [f.finding_id]: new Set(all ? [] : f.claims.map((_, i) => i + 1)) };
    });

  async function open(e: Evidence) {
    if (!e.path || !props.projectId) return;
    try {
      const r = await api.projectFile(props.projectId, e.path, e.range);
      setSource({
        label: evidenceLabel(e),
        lines: r.lines,
        first: r.first_line,
        note: r.truncated ? "Shown up to the display limit." : undefined,
      });
    } catch (err) {
      setSource({ label: evidenceLabel(e), lines: [], first: 1, note: err instanceof ApiError ? err.message : "Can't open" });
    }
  }

  const picks = findings
    .filter((f) => (selected[f.finding_id]?.size ?? 0) > 0)
    .map((f) => {
      const claims = [...selected[f.finding_id]].sort((a, b) => a - b);
      return { id: f.finding_id, version: f.version, claims: claims.length === f.claims.length ? null : claims };
    });

  if (!findings.length) return <p className="empty">No findings for this task yet.</p>;

  return (
    <section className="compare" aria-labelledby="compare-title">
      <div className="compare-head">
        <h3 id="compare-title">Findings</h3>
        <button className="primary" disabled={!picks.length} onClick={() => setSharing(true)}>
          Send selected to a session…
        </button>
      </div>
      <div className="compare-grid">
        {findings.map((f) => (
          <article key={f.finding_id} className="finding-card" aria-label={`Finding by ${who(f)}`}>
            <header>
              <label className="inline">
                <input
                  type="checkbox"
                  aria-label={`Select all claims of ${f.finding_id}`}
                  checked={(selected[f.finding_id]?.size ?? 0) === f.claims.length}
                  onChange={() => toggleAll(f)}
                />
                <strong>{who(f)}</strong>
              </label>
              <span className="muted">
                v{f.version} · via {f.publication_path}
              </span>
            </header>
            <p className="summary">{f.summary}</p>
            <ol className="claims">
              {f.claims.map((c, i) => (
                <li key={i}>
                  <label className="inline claim">
                    <input
                      type="checkbox"
                      checked={selected[f.finding_id]?.has(i + 1) ?? false}
                      onChange={() => toggle(f.finding_id, i + 1)}
                    />
                    <span>{c.text}</span>
                  </label>
                  <div className="evidence-list">
                    {c.evidence.length ? (
                      c.evidence.map((e, j) => (
                        <button key={j} className="link mono" onClick={() => void open(e)} disabled={!e.path}>
                          {evidenceLabel(e)}
                        </button>
                      ))
                    ) : (
                      <span className="warn-text">unsupported</span>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </article>
        ))}
      </div>
      {source && (
        <figure className="source-view">
          <figcaption>
            {source.label} <button className="link" onClick={() => setSource(null)}>close</button>
          </figcaption>
          {source.note && <p className="hint">{source.note}</p>}
          <pre>
            {source.lines.map((l, i) => `${String(source.first + i).padStart(5)}  ${l}`).join("\n")}
          </pre>
        </figure>
      )}
      {sharing && (
        <SharePanel
          taskId={props.taskId}
          picks={picks}
          sessions={props.sessions}
          onClose={() => setSharing(false)}
        />
      )}
      {syntheses.length > 0 && (
        <>
          <h3>Syntheses</h3>
          {syntheses.map((s) => (
            <details key={s.artifact_id} className="synthesis">
              <summary>
                {s.content.title} · by {s.content.author?.runtime ?? "an agent"} ·{" "}
                <span className="muted">{s.content.label}</span>
              </summary>
              <p className="preview">{String(s.content.text ?? "")}</p>
            </details>
          ))}
        </>
      )}
    </section>
  );
}

export function SharePanel(props: {
  taskId: string;
  picks: { id: string; version: number; claims: number[] | null }[];
  sessions: Session[];
  onClose: () => void;
}) {
  const targets = props.sessions.filter((s) => s.mode === "managed" && !s.archived && s.runtime_state !== "stopped");
  const [target, setTarget] = useState(targets[0]?.session_id ?? "");
  const [question, setQuestion] = useState(props.picks.length > 1 ? SYNTHESIS : "");
  const [packet, setPacket] = useState<Packet | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  async function run(label: string, fn: () => Promise<string | void>) {
    setStatus(`${label}…`);
    try {
      setStatus((await fn()) ?? null);
    } catch (e) {
      setStatus(e instanceof ApiError ? `${label}: ${e.message}` : `${label} failed`);
    }
  }

  const [allowLoops, setAllowLoops] = useState<string[]>([]);
  const preview = (allow = allowLoops) =>
    run("Preview", async () =>
      setPacket(
        await api.previewContext({
          task_id: props.taskId,
          target_session_id: target,
          question,
          findings: props.picks,
          include_objective: true,
          allow_loops: allow,
        }),
      ),
    );
  const includeAnyway = (id: string) => {
    const allow = [...allowLoops, id];
    setAllowLoops(allow);
    void preview(allow);
  };
  const send = () =>
    run("Send", async () => {
      if (!packet) return;
      await api.confirmContext(packet.packet_id, packet.payload_hash);
      setPacket(await api.deliverContext(packet.packet_id));
    });
  const refresh = () => packet && void api.getContext(packet.packet_id).then(setPacket);
  const save = () =>
    run("Save reply", async () => {
      if (!packet) return;
      const r = await api.saveReply(packet.packet_id);
      return `Saved as synthesis ${r.artifact_id}`;
    });

  const delivered = packet?.deliveries.find((d) => d.state !== "failed");
  return (
    <section className="share" aria-labelledby="share-title">
      <h3 id="share-title">Send to a session</h3>
      {!packet ? (
        <>
          <label htmlFor="share-target">Recipient</label>
          <select id="share-target" value={target} onChange={(e) => setTarget(e.target.value)}>
            {targets.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.runtime} · {s.session_id.slice(0, 8)} · {s.runtime_state}
                {s.independence ? " · independent" : ""}
              </option>
            ))}
          </select>
          <label htmlFor="share-question">What should it do with this?</label>
          <textarea id="share-question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} />
          <div className="actions">
            <button className="primary" disabled={!target || !question.trim()} onClick={() => void preview()}>
              Preview
            </button>
            <button onClick={props.onClose}>Cancel</button>
          </div>
        </>
      ) : (
        <>
          <dl className="facts">
            <dt>To</dt>
            <dd>
              {packet.recipient.runtime} session {packet.recipient.session_id}
            </dd>
            <dt>Size</dt>
            <dd>
              {packet.bytes.toLocaleString()} bytes, about {packet.approx_tokens.toLocaleString()} tokens (approximate)
            </dd>
            <dt>State</dt>
            <dd>{packet.state}</dd>
          </dl>
          <ol className="sources">
            {packet.sources.map((s) => (
              <li key={s.n}>{s.label}</li>
            ))}
          </ol>
          {packet.excluded.length > 0 && (
            <ul className="excluded" aria-label="Left out">
              {packet.excluded.map((x) => (
                <li key={x.id} className="hint warn">
                  Left out: {x.label} ({x.reason}).{" "}
                  {packet.state === "previewed" && (
                    <button onClick={() => includeAnyway(x.id)}>Include anyway</button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {packet.redactions.length > 0 && (
            <p className="hint warn">
              Redacted: {packet.redactions.map((r) => `${r.rule_id} (source ${r.source})`).join(", ")}
            </p>
          )}
          {packet.flags.map((f) => (
            <p key={f} className="hint warn">
              {f}
            </p>
          ))}
          {packet.text && (
            <details>
              <summary>Exact text that will be sent</summary>
              <pre className="payload">{packet.text}</pre>
            </details>
          )}
          {packet.invalid_reason && <p className="error">{packet.invalid_reason}. Preview again.</p>}
          <div className="actions">
            {packet.state === "previewed" && (
              <button className="primary" onClick={() => void send()}>
                Confirm and send
              </button>
            )}
            {delivered && (
              <>
                <span className="muted">Delivery: {delivered.state}</span>
                <button onClick={refresh}>Refresh</button>
                <button onClick={() => void save()}>Save reply as synthesis</button>
              </>
            )}
            <button onClick={props.onClose}>Close</button>
          </div>
        </>
      )}
      <p className="status" role="status" aria-live="polite">
        {status}
      </p>
    </section>
  );
}
