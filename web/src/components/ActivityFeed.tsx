import type { FeedItem } from "../useWorkspace";

const EVIDENCE: Record<FeedItem["evidence_kind"], string> = {
  reported: "reported",
  observed: "observed",
  inferred: "inferred",
  unknown: "unknown",
};

/** Agent text is rendered as plain text only: no HTML, no links, no remote content (TR-070). */
export function ActivityFeed(props: { items: FeedItem[]; sessionId: string | null; ready: boolean }) {
  const items = props.sessionId ? props.items.filter((i) => i.session_id === props.sessionId) : props.items;
  return (
    <section className="feed" aria-labelledby="feed-title">
      <h2 id="feed-title">Activity</h2>
      {!props.ready ? (
        <p className="empty">Loading…</p>
      ) : !items.length ? (
        <p className="empty">No activity since this page opened. Earlier history is kept on the server.</p>
      ) : (
        <ol className="feed-list">
          {items.map((i) => (
            <li key={i.event_id} className={`feed-item fam-${i.event_family}`}>
              <time dateTime={i.source_time ?? i.ingested_at}>
                {new Date(i.source_time ?? i.ingested_at).toLocaleTimeString()}
              </time>
              <span className="type">{i.event_type}</span>
              <span className="evidence" title="How Robin knows this">
                {EVIDENCE[i.evidence_kind]}
              </span>
              {i.preview && <p className="preview">{i.preview}</p>}
              {i.payload_ref && <p className="preview muted">Large payload stored by reference</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
