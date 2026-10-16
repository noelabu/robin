import type { MapCard, MapCluster } from "../api";

export const CARD = { w: 248, h: 128 };
export const SUB = { w: 212, h: 86 };
const GAP = 16;
const COL_GAP = 72;
const PAD = 24;
const HEADER = 20;
const CLUSTER_GAP = 64;
const ROW_WIDTH = 2400;
const MORE_H = 40;
const COLUMN_HEIGHT = 760; // a cluster wraps into another column past this
const COLUMN_GAP = 32;

export type Rect = { x: number; y: number; w: number; h: number };
export type Frame = Rect & { key: string; more: (Rect & { count: number }) | null };
export type Edge = { from: string; to: string; d: string; live: boolean };
export type Layout = { cards: Record<string, Rect>; frames: Frame[]; edges: Edge[]; bounds: { w: number; h: number } };

/**
 * Clusters are packed left to right in rows (shelf packing), in the order given. Inside a cluster, sessions
 * stack in columns in the server's order, wrapping past COLUMN_HEIGHT; each session's subagents stack beside it.
 */
export function layout(order: string[], clusters: MapCluster[], cards: MapCard[]): Layout {
  const byKey = new Map(clusters.map((c) => [c.key, c]));
  const mains = new Map<string, MapCard[]>();
  const subs = new Map<string, MapCard[]>();
  for (const c of cards) {
    if (c.parent) subs.set(c.parent, [...(subs.get(c.parent) ?? []), c]);
    else mains.set(c.cluster, [...(mains.get(c.cluster) ?? []), c]);
  }
  const out: Layout = { cards: {}, frames: [], edges: [], bounds: { w: 0, h: 0 } };
  let x = 0;
  let y = 0;
  let rowH = 0;
  for (const key of order) {
    const cluster = byKey.get(key);
    const list = mains.get(key) ?? [];
    if (!cluster || !list.length) continue;
    // Place into a cluster at the origin, then move it into its row once its size is known.
    const placed: Record<string, Rect> = {};
    const top = PAD + HEADER;
    let colX = PAD;
    let colY = top;
    let colW = 0;
    let bottom = top;
    const slot = (w: number, h: number) => {
      if (colY > top && colY + h > top + COLUMN_HEIGHT) {
        colX += colW + COLUMN_GAP;
        colY = top;
        colW = 0;
      }
      const at = { x: colX, y: colY };
      colY += h + GAP;
      colW = Math.max(colW, w);
      bottom = Math.max(bottom, colY - GAP);
      return at;
    };
    for (const card of list) {
      const kids = subs.get(card.session_id) ?? [];
      const at = slot(CARD.w + (kids.length ? COL_GAP + SUB.w : 0), Math.max(CARD.h, kids.length * (SUB.h + GAP) - GAP));
      placed[card.id] = { x: at.x, y: at.y, w: CARD.w, h: CARD.h };
      kids.forEach((k, i) => {
        placed[k.id] = { x: at.x + CARD.w + COL_GAP, y: at.y + i * (SUB.h + GAP), w: SUB.w, h: SUB.h };
      });
    }
    const hidden = cluster.sessions - cluster.shown;
    const moreAt = hidden > 0 ? slot(CARD.w, MORE_H) : null;
    const w = colX + colW + PAD;
    const h = bottom + PAD;
    if (x > 0 && x + w > ROW_WIDTH) {
      x = 0;
      y += rowH + CLUSTER_GAP;
      rowH = 0;
    }
    for (const [id, r] of Object.entries(placed)) out.cards[id] = { ...r, x: r.x + x, y: r.y + y };
    const more = moreAt ? { x: moreAt.x + x, y: moreAt.y + y, w: CARD.w, h: MORE_H, count: hidden } : null;
    const frame: Frame = { key, x, y, w, h, more };
    out.frames.push(frame);
    rowH = Math.max(rowH, frame.h);
    x += w + CLUSTER_GAP;
    out.bounds.w = Math.max(out.bounds.w, frame.x + frame.w);
    out.bounds.h = Math.max(out.bounds.h, frame.y + frame.h);
  }
  for (const c of cards) {
    const a = c.parent ? out.cards[c.parent] : undefined;
    const b = out.cards[c.id];
    if (!a || !b) continue;
    const x1 = a.x + a.w;
    const y1 = a.y + 40;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const mx = (x1 + x2) / 2;
    out.edges.push({ from: c.parent!, to: c.id, d: `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`, live: c.state === "running" });
  }
  return out;
}

/** Keep the first-seen cluster order so cards don't jump under the cursor; new clusters go at the end. */
export function stableOrder(previous: string[], clusters: MapCluster[]): string[] {
  const keys = new Set(clusters.map((c) => c.key));
  const kept = previous.filter((k) => keys.has(k));
  const seen = new Set(kept);
  return [...kept, ...clusters.map((c) => c.key).filter((k) => !seen.has(k))];
}
