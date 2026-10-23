import type { MapCard, MapCluster } from "../api";
import { CARD, size, type Rect } from "../map/layout";

const PAD = 12;
const INSET = 24; // a child sits this far in from its parent on each side; the parent's trunk runs in the gap
const GAP = 28; // between a card and what hangs below it
const SESSION_GAP = 36;
const MIN_W = 180;
const PILL = { w: 128, h: 30 };
const CELL_H = 86;
const CELL_GAP = 12;

export type PhoneRect = Rect & { small?: boolean };
export type Fold = Rect & { parent: string; count: number; open: boolean };
export type PhoneWire = { d: string; kind: "q" | "wf" | ""; head: { x: number; y: number; dir: "down" | "right" | "left" } | null };
export type PhoneLayout = { cards: Record<string, PhoneRect>; folds: Fold[]; wires: PhoneWire[]; height: number };

const finished = (c: MapCard) => c.status === "completed" || c.status === "killed";

/**
 * One folder's cards top to bottom for a phone: each session full width, what it started below it, inset,
 * on a wire. Finished children fold into a pill that opens into a two-column grid; a workflow's agents are
 * left to the workflow card's summary.
 */
export function phoneLayout(
  cards: MapCard[],
  opts: { width: number; open: ReadonlySet<string>; needs: (c: MapCard) => boolean },
): PhoneLayout {
  const byId = new Map(cards.map((c) => [c.id, c]));
  const kids = new Map<string, MapCard[]>();
  for (const c of cards)
    if (c.parent && byId.get(c.parent)?.kind !== "workflow") kids.set(c.parent, [...(kids.get(c.parent) ?? []), c]);
  const out: PhoneLayout = { cards: {}, folds: [], wires: [], height: 0 };
  const right = opts.width - PAD;

  function place(card: MapCard, r: Rect): number {
    out.cards[card.id] = r;
    let y = r.y + r.h;
    const list = kids.get(card.id) ?? [];
    const live = list.filter((c) => !finished(c));
    const done = list.filter(finished);
    const cx = r.x + INSET;
    const cw = Math.min(Math.max(MIN_W, r.w - 2 * INSET), right - cx);
    const hangers: { rect: Rect; kind: PhoneWire["kind"] }[] = [];
    const q = opts.needs(card) ? "q" : "";
    for (const c of live) {
      const cr = { x: cx, y: y + GAP, w: cw, h: size(c, card).h };
      hangers.push({ rect: cr, kind: q || (c.kind === "workflow" ? "wf" : "") });
      y = place(c, cr);
    }
    if (done.length) {
      const open = opts.open.has(card.id);
      const px = live.length ? cx : r.x + (r.w - PILL.w) / 2;
      const pill: Fold = { x: px, y: y + GAP, w: PILL.w, h: PILL.h, parent: card.id, count: done.length, open };
      out.folds.push(pill);
      hangers.push({ rect: pill, kind: q });
      y = pill.y + pill.h;
      if (open) {
        const top = y + GAP;
        const colW = (r.w - CELL_GAP) / 2;
        const mid = r.x + r.w / 2;
        const rows = Math.ceil(done.length / 2);
        done.forEach((c, i) => {
          const cell = { x: r.x + (i % 2) * (colW + CELL_GAP), y: top + Math.floor(i / 2) * (CELL_H + CELL_GAP), w: colW, h: CELL_H, small: true };
          out.cards[c.id] = cell;
          const cy = cell.y + CELL_H / 2;
          const to = i % 2 ? cell.x : cell.x + cell.w;
          out.wires.push({ d: `M${mid} ${cy} H${to}`, kind: "", head: { x: to, y: cy, dir: i % 2 ? "right" : "left" } });
        });
        // The grid's trunk: from the pill down the gutter between the columns.
        const lastMid = top + (rows - 1) * (CELL_H + CELL_GAP) + CELL_H / 2;
        const pc = pill.x + pill.w / 2;
        out.wires.push({
          d: `M${pc} ${pill.y + pill.h} V${top - 12} H${mid} V${lastMid}`,
          kind: "",
          head: null,
        });
        y = top + rows * CELL_H + (rows - 1) * CELL_GAP;
      }
    }
    const x1 = r.x + r.w / 2;
    const y1 = r.y + r.h;
    if (hangers.length === 1) {
      const { rect: h, kind } = hangers[0];
      const x2 = h.x + h.w / 2;
      out.wires.push({
        d: `M${x1} ${y1} C${x1} ${y1 + 16}, ${x2} ${h.y - 16}, ${x2} ${h.y}`,
        kind,
        head: { x: x2, y: h.y, dir: "down" },
      });
    } else if (hangers.length > 1) {
      // Several: a trunk down the left gap, a branch into each one's left edge.
      const tx = r.x + INSET / 2;
      const last = hangers[hangers.length - 1].rect;
      out.wires.push({ d: `M${tx} ${y1} V${last.y + last.h / 2}`, kind: q, head: null });
      for (const { rect: h, kind } of hangers) {
        const hy = h.y + h.h / 2;
        out.wires.push({ d: `M${tx} ${hy} H${h.x}`, kind, head: { x: h.x, y: hy, dir: "right" } });
      }
    }
    return y;
  }

  let y = PAD;
  for (const c of cards.filter((c) => !c.parent)) {
    y = place(c, { x: PAD, y, w: opts.width - 2 * PAD, h: CARD.h }) + SESSION_GAP;
  }
  out.height = Math.max(y - SESSION_GAP + PAD, 0);
  return out;
}

/** How many sessions in each folder are waiting on you. */
export function folderNeeds(mains: MapCard[], needs: (c: MapCard) => boolean): Record<string, number> {
  const out: Record<string, number> = {};
  for (const c of mains) if (needs(c)) out[c.cluster] = (out[c.cluster] ?? 0) + 1;
  return out;
}

/** The folder to show: the one chosen while it's still there, else one that needs you, else one working, else the first. */
export function pickFolder(clusters: MapCluster[], needs: Record<string, number>, chosen: string | null): string | null {
  if (chosen && clusters.some((c) => c.key === chosen)) return chosen;
  return (clusters.find((c) => needs[c.key]) ?? clusters.find((c) => c.running > 0) ?? clusters[0])?.key ?? null;
}
