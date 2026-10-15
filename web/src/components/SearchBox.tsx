import { useEffect, useRef, useState } from "react";
import { api, ApiError, type SearchResult } from "../api";

/** Search across sessions, tasks and findings. Results are plain text; secrets were masked when indexed. */
export function SearchBox(props: { onOpenSession: (id: string) => void; onOpenTask: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    window.clearTimeout(timer.current);
    if (!q.trim()) {
      setResults(null);
      return;
    }
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.search(q);
        setResults(r.results);
        setNote(r.pending_events ? `${r.pending_events} newest events aren't searchable yet` : null);
      } catch (e) {
        setResults([]);
        setNote(e instanceof ApiError ? e.message : "Search failed");
      }
    }, 250);
  }, [q]);

  function open(r: SearchResult) {
    if (r.kind === "task") props.onOpenTask(r.ref);
    else if (r.kind === "finding" || r.kind === "synthesis") r.task_id && props.onOpenTask(r.task_id);
    else if (r.session_id) props.onOpenSession(r.session_id);
    setQ("");
  }

  return (
    <div className="search" role="search">
      <input
        type="search"
        aria-label="Search sessions, tasks and findings"
        placeholder="Search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && setQ("")}
      />
      {results !== null && (
        <div className="search-results" aria-live="polite">
          {!results.length ? (
            <p className="empty">{note ?? "No matches."}</p>
          ) : (
            <ul>
              {results.map((r) => (
                <li key={`${r.kind}:${r.ref}`}>
                  <button className="search-hit" onClick={() => open(r)}>
                    <span className="kind">{r.kind}</span> <strong>{r.title}</strong>
                    <span className="snippet">{r.snippet}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {results.length > 0 && note && <p className="hint">{note}</p>}
        </div>
      )}
    </div>
  );
}
