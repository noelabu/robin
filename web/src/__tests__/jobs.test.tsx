import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { MapCard, MapCluster, SessionActivity } from "../api";
import { Card } from "../map/Card";
import { layout, GROUP, MINI, SHELL } from "../map/layout";
import { TaskPanel } from "../map/TaskPanel";
import { liveState } from "../map/useMap";

const NOW = Date.parse("2026-10-17T10:00:00Z");

const card = (over: Partial<MapCard> = {}): MapCard => ({
  id: "s1",
  kind: "session",
  session_id: "s1",
  parent: null,
  cluster: "/r/app",
  runtime: "claude",
  mode: "observed",
  label: null,
  title: "can you verify if these are good to merge",
  state: "idle",
  state_evidence: "reported",
  detail: true,
  todos: null,
  files: 0,
  last_file: null,
  tool: null,
  target: null,
  action_at: null,
  turn_started_at: null,
  last_activity_at: "2026-10-17T09:59:00Z",
  created_at: null,
  subagents_hidden: 0,
  ...over,
});
const cluster: MapCluster = {
  key: "/r/app",
  label: "app",
  path: "~/r/app",
  sessions: 1,
  shown: 1,
  running: 1,
  waiting: 0,
  last_activity_at: null,
};

const skill = card({ id: "s1:sk", kind: "skill", parent: "s1", label: "Skill", title: "review-lite", inner: ["code-reviewer"], state: "running", state_evidence: "inferred" });
const workflow = card({
  id: "s1:wf",
  kind: "workflow",
  parent: "s1:sk",
  label: "Workflow",
  title: "review-pipeline",
  run: "wf_1",
  state: "running",
  state_evidence: "inferred",
  phases: [
    { name: "Context", done: 1, failed: 0, total: 1 },
    { name: "Find", done: 1, failed: 1, total: 3 },
  ],
});
const agent = (id: string, phase: string, over: Partial<MapCard> = {}) =>
  card({ id: `s1:${id}`, kind: "subagent", parent: "s1:wf", label: "correctness-reviewer", title: id, phase, ...over });
const shell = card({ id: "s1:sh", kind: "shell", parent: "s1", label: "Background shell", title: "Run the full Python suite", target: "uv run pytest -q", state: "running", state_evidence: "inferred" });
const cards = [card(), skill, workflow, agent("agent-a", "Context", { status: "completed", state_evidence: "reported" }), agent("agent-b", "Find"), shell];

describe("layout of what a session started", () => {
  const lay = layout(["/r/app"], [cluster], cards);
  test("session → skill → workflow → its agents, each a column to the right", () => {
    const [s, sk, wf, a, b, sh] = cards.map((c) => lay.cards[c.id]);
    expect(sk.x).toBeGreaterThan(s.x + s.w);
    expect(wf.x).toBeGreaterThan(sk.x + sk.w);
    expect(a.x).toBeGreaterThan(wf.x + wf.w);
    expect([sk.w, wf.w, a.w, sh.w]).toEqual([GROUP.w, GROUP.w, MINI.w, SHELL.w]);
    expect(b.y).toBeGreaterThan(a.y);
    expect(sh.y).toBeGreaterThanOrEqual(b.y + b.h); // the shell goes below the skill's whole subtree
  });
  test("a wire joins each card to the one that started it", () => {
    expect(lay.edges.map((e) => `${e.from}>${e.to}`).sort()).toEqual(
      ["s1>s1:sk", "s1:sk>s1:wf", "s1:wf>s1:agent-a", "s1:wf>s1:agent-b", "s1>s1:sh"].sort(),
    );
  });
});

describe("job cards", () => {
  const show = (c: MapCard) => {
    const s = liveState(c, undefined, NOW);
    render(<Card card={c} rect={{ x: 0, y: 0, w: 248, h: 96 }} state={s.state} inferred={s.inferred} now={NOW} selected={false} onOpen={() => {}} onFocus={() => {}} />);
    return document.querySelector("[data-card]") as HTMLElement;
  };
  test("a workflow shows its phases and keeps the server's state though its activity is old", () => {
    const el = show({ ...workflow, last_activity_at: "2026-10-17T09:00:00Z" });
    expect(el.className).toContain("workflow");
    expect(el.textContent).toContain("Context 1/1");
    expect(el.textContent).toContain("Find 1/3 · 1 failed");
    expect(el.textContent).toContain("Working");
  });
  test("a skill names the skills it called", () => {
    expect(show(skill).textContent).toContain("→ code-reviewer");
  });
  test("a workflow agent reads its phase; a finished one says Completed", () => {
    const el = show(agent("agent-a", "Context", { status: "completed", state_evidence: "reported" }));
    expect(el.className).toContain("mini");
    expect(el.textContent).toContain("Context · correctness-reviewer");
    expect(el.textContent).toContain("Completed");
  });
  test("a shell shows its command", () => {
    expect(show(shell).textContent).toContain("uv run pytest -q");
  });
});

test("a workflow's panel lists its agents by phase with how each ended", async () => {
  const detail: SessionActivity = {
    session_id: "s1",
    cluster: "/r/app",
    lanes: [],
    jobs: [],
    runs: {
      wf_1: {
        a: { phase: "Context", label: "context", status: "completed" },
        b: { phase: "Find", label: "find:correctness", status: null },
        c: { phase: "Find", label: "find:contracts", status: "failed" },
      },
    },
  };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(detail), { status: 200 })));
  render(<TaskPanel card={workflow} state="running" now={NOW} onHeight={() => {}} onClose={() => {}} />);
  expect(await screen.findByText("find:correctness")).toBeTruthy();
  expect(screen.getByText("Working")).toBeTruthy();
  expect(screen.getByText("Failed")).toBeTruthy();
  expect(screen.getByText("0/2 · 1 failed")).toBeTruthy();
  vi.unstubAllGlobals();
});

test("a workflow's agents line up in phase order, whatever order they arrive in", () => {
  const verify = agent("agent-v", "Verify", { last_activity_at: "2026-10-17T09:59:50Z" });
  const find = agent("agent-f", "Find", { last_activity_at: "2026-10-17T09:59:30Z" });
  const ctx = agent("agent-c", "Context", { last_activity_at: "2026-10-17T09:59:10Z" });
  const wf = { ...workflow, phases: [...(workflow.phases ?? []), { name: "Verify", done: 0, failed: 0, total: 1 }] };
  const lay = layout(["/r/app"], [cluster], [card(), skill, wf, verify, find, ctx]);
  const ys = [ctx, find, verify].map((c) => lay.cards[c.id].y);
  expect(ys).toEqual([...ys].sort((a, b) => a - b));
});
