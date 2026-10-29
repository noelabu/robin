import { useEffect, useRef, useState } from "react";
import type { DiffHunk, FileDiff as Diff } from "../api";
import { Sheet } from "../phone/Sheet";

export type DiffLayout = "unified" | "split";

const KEY = "robin.diff";

function saved(): DiffLayout {
  try {
    return localStorage.getItem(KEY) === "split" ? "split" : "unified";
  } catch {
    return "unified";
  }
}

/** Unified or split, remembered in this browser. */
export function useDiffLayout(): [DiffLayout, (l: DiffLayout) => void] {
  const [layout, setLayout] = useState<DiffLayout>(saved);
  return [
    layout,
    (next) => {
      setLayout(next);
      try {
        localStorage.setItem(KEY, next);
      } catch {
        // not remembered; still applied
      }
    },
  ];
}

const STATUS: Record<Exclude<Diff["status"], "ok">, string> = {
  binary: "Binary file.",
  too_large: "Too large to show.",
  not_recorded: "Diff not available for this run.",
  unavailable: "No longer available.",
  unknown: "Couldn't read Git.",
};

type Row = { kind: " " | "+" | "-" | "\\"; text: string; old: number | null; new: number | null };

function rows(h: DiffHunk): Row[] {
  let o = h.old_start;
  let n = h.new_start;
  return h.lines.map((line) => {
    const kind = line[0] as Row["kind"];
    const text = line.slice(1);
    if (kind === "\\") return { kind, text: line, old: null, new: null };
    if (kind === "-") return { kind, text, old: o++, new: null };
    if (kind === "+") return { kind, text, old: null, new: n++ };
    return { kind, text, old: o++, new: n++ };
  });
}

const CLASS = { " ": "ctx", "+": "add", "-": "del", "\\": "note" } as const;

function Unified({ hunk }: { hunk: DiffHunk }) {
  return (
    <>
      {rows(hunk).map((r, i) => (
        <tr key={i} className={CLASS[r.kind]}>
          <td className="ln">{r.old ?? ""}</td>
          <td className="ln">{r.new ?? ""}</td>
          <td className="code">
            {r.kind === "\\" ? r.text : r.kind}
            {r.kind !== "\\" && r.text}
          </td>
        </tr>
      ))}
    </>
  );
}

/** Removed lines on the left, added on the right, paired in order within each run of changes. */
function Split({ hunk }: { hunk: DiffHunk }) {
  const out: [Row | null, Row | null][] = [];
  let del: Row[] = [];
  let add: Row[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(del.length, add.length); i++) out.push([del[i] ?? null, add[i] ?? null]);
    del = [];
    add = [];
  };
  for (const r of rows(hunk)) {
    if (r.kind === "-") del.push(r);
    else if (r.kind === "+") add.push(r);
    else {
      flush();
      out.push(r.kind === "\\" ? [r, null] : [r, r]);
    }
  }
  flush();
  const side = (r: Row | null, which: "old" | "new") =>
    r ? (
      <>
        <td className={`ln ${CLASS[r.kind]}`}>{r[which] ?? ""}</td>
        <td className={`code ${CLASS[r.kind]}`}>{r.text}</td>
      </>
    ) : (
      <>
        <td className="ln empty" />
        <td className="code empty" />
      </>
    );
  return (
    <>
      {out.map(([l, r], i) => (
        <tr key={i}>
          {side(l, "old")}
          {side(r, "new")}
        </tr>
      ))}
    </>
  );
}

export function DiffView({ diff, layout }: { diff: Diff; layout: DiffLayout }) {
  if (diff.status !== "ok") return <p className="muted">{STATUS[diff.status]}</p>;
  if (!diff.hunks.length) return <p className="muted">No changes.</p>;
  return (
    <div className="diff-scroll">
      <table className={`diff ${layout}`}>
        {diff.hunks.map((h, i) => (
          <tbody key={i}>
            <tr className="hunk">
              <td colSpan={layout === "split" ? 4 : 3}>
                @@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@ {h.header}
              </td>
            </tr>
            {layout === "split" ? <Split hunk={h} /> : <Unified hunk={h} />}
          </tbody>
        ))}
      </table>
      {diff.truncated && <p className="hint">Showing the first 5,000 lines.</p>}
    </div>
  );
}

/** One file's diff, loaded when shown: inline with a layout switch, or full screen and unified on a phone. */
export function FileDiff(props: { path: string; load: () => Promise<Diff>; compact?: boolean; onClose: () => void }) {
  const [diff, setDiff] = useState<Diff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useDiffLayout();
  const load = useRef(props.load);
  load.current = props.load;
  useEffect(() => {
    let live = true;
    load
      .current()
      .then((d) => live && setDiff(d))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.path]);

  const body = error ? (
    <p className="hint warn">{error}</p>
  ) : diff ? (
    <DiffView diff={diff} layout={props.compact ? "unified" : layout} />
  ) : (
    <p className="muted">Loading…</p>
  );
  if (props.compact)
    return (
      <Sheet full label={props.path} onClose={props.onClose}>
        {body}
      </Sheet>
    );
  return (
    <div className="file-diff">
      <div className="diff-bar">
        {diff?.status === "ok" && (
          <span className="muted">
            +{diff.added} −{diff.removed}
          </span>
        )}
        <span className="seg" role="group" aria-label="Diff layout">
          {(["unified", "split"] as const).map((l) => (
            <button key={l} type="button" aria-pressed={layout === l} onClick={() => setLayout(l)}>
              {l === "unified" ? "Unified" : "Split"}
            </button>
          ))}
        </span>
      </div>
      {body}
    </div>
  );
}
