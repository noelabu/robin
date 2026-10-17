import type { MapCard, MapCluster } from "../api";

export const CARD = { w: 272, h: 128 };
export const SUB = { w: 228, h: 86 };
export const GROUP = { w: 248, h: 96 }; // a skill or a workflow
export const SHELL = { w: 228, h: 72 };
export const MINI = { w: 208, h: 60 }; // a workflow's agent
export const PANEL_GAP = 8; // between a card and its open task panel
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
export type Edge = { from: string; to: string; d: string; live: boolean; cluster: string };
export type Layout = { cards: Record<string, Rect>; frames: Frame[]; edges: Edge[]; bounds: { w: number; h: number } };

/** A card's size on the canvas, by what it is. */
export function size(card: MapCard, parent: MapCard | undefined): { w: number; h: number } {
  if (!card.parent) return CARD;
  if (card.kind === "skill" || card.kind === "workflow") return GROUP;
  if (card.kind === "shell") return SHELL;
  return parent?.kind === "workflow" ? MINI : SUB;
}

/**
 * Clusters are packed left to right in rows (shelf packing), in the order given. Inside a cluster, sessions
 * stack in columns in the server's order, wrapping past COLUMN_HEIGHT. Whatever a card started (subagents,
 * skills, workflows, shells) stacks in a column beside it, and so on for theirs: session → skill → workflow →
 * its agents.
 */
export function layout(
  order: string[],
  clusters: MapCluster[],
  cards: MapCard[],
  below: Record<string, number> = {}, // card id -> height of the task panel open under it
): Layout {
  const byKey = new Map(clusters.map((c) => [c.key, c]));
  const mains = new Map<string, MapCard[]>();
  const subs = new Map<string, MapCard[]>();
  const byId = new Map(cards.map((c) => [c.id, c]));
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
    const room = (id: string) => (below[id] ? below[id] + PANEL_GAP : 0);
    // A card and everything under it: its size, then where each card goes relative to its top left.
    const tree = (card: MapCard, seen: Set<string>): { w: number; h: number; at: Record<string, Rect> } => {
      const own = size(card, card.parent ? byId.get(card.parent) : undefined);
      let kids = (subs.get(card.id) ?? []).filter((k) => !seen.has(k.id));
      if (card.kind === "workflow") kids = byPhase(card, kids);
      kids.forEach((k) => seen.add(k.id));
      const at: Record<string, Rect> = { [card.id]: { x: 0, y: 0, ...own } };
      let ky = 0;
      let kw = 0;
      for (const k of kids) {
        const t = tree(k, seen);
        for (const [id, r] of Object.entries(t.at)) at[id] = { ...r, x: r.x + own.w + COL_GAP, y: r.y + ky };
        ky += t.h + GAP;
        kw = Math.max(kw, t.w);
      }
      const kidsH = kids.length ? ky - GAP : 0;
      return { w: own.w + (kids.length ? COL_GAP + kw : 0), h: Math.max(own.h + room(card.id), kidsH), at };
    };
    for (const card of list) {
      const t = tree(card, new Set([card.id]));
      const at = slot(t.w, t.h);
      for (const [id, r] of Object.entries(t.at)) placed[id] = { ...r, x: r.x + at.x, y: r.y + at.y };
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
    const y1 = a.y + Math.min(40, a.h / 2);
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const mx = (x1 + x2) / 2;
    out.edges.push({ from: c.parent!, to: c.id, d: `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`, live: c.state === "running", cluster: c.cluster });
  }
  return out;
}

/** A workflow's agents in the order its phases ran (Context, Find, Verify), oldest first within a phase. */
function byPhase(workflow: MapCard, agents: MapCard[]): MapCard[] {
  const order = (workflow.phases ?? []).map((p) => p.name);
  const rank = (a: MapCard) => (order.includes(a.phase ?? "") ? order.indexOf(a.phase ?? "") : order.length);
  return [...agents].sort((a, b) => rank(a) - rank(b) || (a.last_activity_at ?? "").localeCompare(b.last_activity_at ?? ""));
}

/** Keep the first-seen cluster order so cards don't jump under the cursor; new clusters go at the end. */
export function stableOrder(previous: string[], clusters: MapCluster[]): string[] {
  const keys = new Set(clusters.map((c) => c.key));
  const kept = previous.filter((k) => keys.has(k));
  const seen = new Set(kept);
  return [...kept, ...clusters.map((c) => c.key).filter((k) => !seen.has(k))];
}
