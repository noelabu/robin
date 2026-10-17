import type { ReactNode } from "react";

/**
 * Agent text with its Markdown structure shown, built as React elements from the text: headings, lists,
 * tables, code blocks, inline code and bold. No HTML is parsed and nothing becomes a link (threat model: text
 * nodes only, no Markdown renderer, no data-built href/src).
 */
export function Prose({ text }: { text: string }) {
  return <div className="prose">{blocks(text)}</div>;
}

const FENCE = /^```/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*([-*+]|\d+[.)])\s+(.*)$/;

function blocks(text: string): ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (FENCE.test(line)) {
      const code: string[] = [];
      for (i++; i < lines.length && !FENCE.test(lines[i]); i++) code.push(lines[i]);
      i++; // the closing fence (or the end)
      out.push(
        <pre key={out.length}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    if (line.trimStart().startsWith("|")) {
      const rows: string[][] = [];
      let header = false;
      for (; i < lines.length && lines[i].trimStart().startsWith("|"); i++) {
        if (/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) header = rows.length === 1;
        else rows.push(cells(lines[i]));
      }
      const head = header ? rows[0] : null;
      const body = header ? rows.slice(1) : rows;
      out.push(
        <div key={out.length} className="table-wrap">
          <table>
            {head && (
              <thead>
                <tr>
                  {head.map((c, k) => (
                    <th key={k}>{inline(c)}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {body.map((r, k) => (
                <tr key={k}>
                  {r.map((c, j) => (
                    <td key={j}>{inline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const h = HEADING.exec(line);
    if (h) {
      out.push(
        <p key={out.length} className={`h h${Math.min(h[1].length, 3)}`}>
          {inline(h[2])}
        </p>,
      );
      i++;
      continue;
    }
    if (BULLET.test(line)) {
      const items: { ordered: boolean; text: string; depth: number }[] = [];
      for (; i < lines.length && BULLET.test(lines[i]); i++) {
        const m = BULLET.exec(lines[i])!;
        items.push({ ordered: /\d/.test(m[1]), text: m[2], depth: lines[i].search(/\S/) >= 2 ? 1 : 0 });
      }
      const List = items[0].ordered ? "ol" : "ul";
      out.push(
        <List key={out.length}>
          {items.map((it, k) => (
            <li key={k} className={it.depth ? "nested" : undefined}>
              {inline(it.text)}
            </li>
          ))}
        </List>,
      );
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    for (; i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i]) && !BULLET.test(lines[i]); i++)
      para.push(lines[i]);
    out.push(<p key={out.length}>{inline(para.join("\n"))}</p>);
  }
  return out;
}

const cells = (row: string) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/** `code` and **bold**; everything else stays as written. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] !== undefined ? <code key={m.index}>{m[1]}</code> : <strong key={m.index}>{m[2]}</strong>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
