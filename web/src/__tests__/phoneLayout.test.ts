import type { MapCard, MapCluster } from "../api";
import { folderNeeds, phoneLayout, pickFolder } from "../phone/phoneLayout";

const card = (over: Partial<MapCard> = {}): MapCard => ({
  id: "s1",
  session_id: "s1",
  parent: null,
  cluster: "/r/app",
  runtime: "claude",
  mode: "observed",
  label: null,
  title: "Check the ticket",
  state: "idle",
  state_evidence: "inferred",
  detail: true,
  todos: null,
  files: 0,
  last_file: null,
  tool: null,
  target: null,
  action_at: null,
  turn_started_at: null,
  last_activity_at: "2026-10-17T09:59:55Z",
  created_at: "2026-10-17T09:00:00Z",
  subagents_hidden: 0,
  ...over,
});
const cluster = (over: Partial<MapCluster> = {}): MapCluster => ({
  key: "/r/app",
  label: "app",
  path: "~/r/app",
  sessions: 1,
  shown: 1,
  running: 0,
  waiting: 0,
  last_activity_at: null,
  ...over,
});
const child = (id: string, over: Partial<MapCard> = {}) => card({ id, parent: "s1", kind: "subagent", ...over });

const overlaps = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const none = { width: 320, open: new Set<string>(), needs: () => false };

describe("phone map layout", () => {
  test("cards take their measured heights, and what hangs below follows them", () => {
    const tall = phoneLayout([card(), child("a"), card({ id: "s2", session_id: "s2" })], none);
    const fit = phoneLayout([card(), child("a"), card({ id: "s2", session_id: "s2" })], { ...none, heights: { s1: 90, a: 60 } });
    expect(fit.cards.s1.h).toBe(90);
    expect(fit.cards.a.h).toBe(60);
    expect(fit.cards.a.y).toBe(tall.cards.a.y - (tall.cards.s1.h - 90));
    expect(fit.cards.s2.y).toBe(fit.cards.a.y + 60 + (tall.cards.s2.y - tall.cards.a.y - tall.cards.a.h));
    expect(fit.cards.s2.h).toBe(tall.cards.s2.h); // not measured yet: the canvas's height
  });

  test("a child hangs centred below its session on one wire from the session's bottom", () => {
    const lay = phoneLayout([card(), child("ship", { kind: "skill" })], none);
    const s = lay.cards.s1;
    const c = lay.cards.ship;
    expect(s.w).toBe(320 - 24);
    expect(c.y).toBeGreaterThan(s.y + s.h);
    expect(c.x + c.w / 2).toBe(s.x + s.w / 2);
    expect(lay.wires).toHaveLength(1);
    expect(lay.wires[0].d.startsWith(`M${s.x + s.w / 2} ${s.y + s.h}`)).toBe(true);
    expect(lay.wires[0].head).toEqual({ x: c.x + c.w / 2, y: c.y, dir: "down" });
  });

  test("completed and killed children fold into one pill; a failed one stays", () => {
    const lay = phoneLayout(
      [card(), child("a", { status: "completed" }), child("b", { status: "killed" }), child("c", { status: "failed" })],
      none,
    );
    expect(lay.cards.a).toBeUndefined();
    expect(lay.cards.b).toBeUndefined();
    expect(lay.cards.c).toBeDefined();
    expect(lay.folds).toEqual([expect.objectContaining({ parent: "s1", count: 2, open: false })]);
    expect(lay.folds[0].y).toBeGreaterThan(lay.cards.c.y + lay.cards.c.h);
  });

  test("an opened fold lays the finished children out two by two below the pill", () => {
    const done = ["a", "b", "c", "d"].map((id) => child(id, { status: "completed" }));
    const lay = phoneLayout([card(), ...done], { ...none, open: new Set(["s1"]) });
    const [a, b, c, d] = ["a", "b", "c", "d"].map((id) => lay.cards[id]);
    expect(a.small).toBe(true);
    expect(a.y).toBe(b.y);
    expect(c.y).toBeGreaterThan(a.y + a.h);
    expect(b.x).toBeGreaterThan(a.x + a.w);
    expect(a.y).toBeGreaterThan(lay.folds[0].y + lay.folds[0].h);
    expect(lay.folds[0].open).toBe(true);
    const rects = [a, b, c, d];
    rects.forEach((r, i) => rects.slice(i + 1).forEach((o) => expect(overlaps(r, o)).toBe(false)));
    expect(lay.height).toBeGreaterThanOrEqual(d.y + d.h);
  });

  test("a workflow's agents aren't placed; the workflow is", () => {
    const lay = phoneLayout(
      [card(), child("wf", { kind: "workflow" }), card({ id: "wf:a", parent: "wf", kind: "subagent", phase: "Find" })],
      none,
    );
    expect(lay.cards.wf).toBeDefined();
    expect(lay.cards["wf:a"]).toBeUndefined();
  });

  test("several children hang off a trunk in the left gap, clear of every card", () => {
    const lay = phoneLayout([card(), child("a"), child("b"), child("c", { status: "completed" })], none);
    const trunk = lay.wires.find((w) => w.head === null)!;
    const tx = Number(trunk.d.split(" ")[0].slice(1));
    expect(tx).toBeGreaterThan(lay.cards.s1.x);
    expect(tx).toBeLessThan(lay.cards.a.x);
    expect(lay.wires.filter((w) => w.head?.dir === "right")).toHaveLength(3); // a, b and the pill
  });

  test("wires out of a session that needs you are marked", () => {
    const lay = phoneLayout([card(), child("a")], { ...none, needs: (c) => c.id === "s1" });
    expect(lay.wires[0].kind).toBe("q");
  });

  test("deeply nested children on a narrow phone stay inside the screen's right edge", () => {
    const chain = ["a", "b", "c", "d"].map((id, i) => card({ id, parent: i ? ["a", "b", "c"][i - 1] : "s1", kind: "subagent" }));
    const lay = phoneLayout([card(), ...chain], { ...none, width: 240 });
    for (const r of Object.values(lay.cards)) expect(r.x + r.w).toBeLessThanOrEqual(240 - 12);
    expect(lay.cards.d.x).toBeGreaterThan(lay.cards.c.x);
  });

  test("sessions stack without overlapping and the height covers them", () => {
    const lay = phoneLayout([card(), child("a"), card({ id: "s2", session_id: "s2" })], none);
    const all = Object.values(lay.cards);
    all.forEach((r, i) => all.slice(i + 1).forEach((o) => expect(overlaps(r, o)).toBe(false)));
    expect(lay.height).toBeGreaterThanOrEqual(Math.max(...all.map((r) => r.y + r.h)));
  });
});

describe("folders", () => {
  test("counts the sessions in each folder that need you", () => {
    const mains = [card(), card({ id: "s2", session_id: "s2" }), card({ id: "s3", session_id: "s3", cluster: "/r/api" })];
    expect(folderNeeds(mains, (c) => c.id !== "s3")).toEqual({ "/r/app": 2 });
  });

  test("keeps the folder chosen, else one that needs you, else one working, else the first", () => {
    const list = [cluster({ key: "a" }), cluster({ key: "b", running: 1 }), cluster({ key: "c" })];
    expect(pickFolder(list, { c: 1 }, "a")).toBe("a");
    expect(pickFolder(list, { c: 1 }, null)).toBe("c");
    expect(pickFolder(list, {}, "gone")).toBe("b");
    expect(pickFolder([cluster({ key: "a" })], {}, null)).toBe("a");
    expect(pickFolder([], {}, null)).toBeNull();
  });
});
