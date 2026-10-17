import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { MapCard, MapCluster, MapSnapshot, SessionActivity } from "../api";
import { Card } from "../map/Card";
import { layout, stableOrder } from "../map/layout";
import { MapView } from "../map/MapView";
import { liveState } from "../map/useMap";
import { Wire } from "../map/Wire";

const H = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
const NOW = Date.parse("2026-10-17T10:00:00Z");

const card = (over: Partial<MapCard> = {}): MapCard => ({
  id: "s1",
  session_id: "s1",
  parent: null,
  cluster: "/r/app",
  runtime: "claude",
  mode: "observed",
  label: null,
  title: "Build the map",
  state: "idle",
  state_evidence: "inferred",
  detail: true,
  todos: { done: 3, total: 5 },
  files: 4,
  last_file: "web/src/App.tsx",
  tool: "Edit",
  target: "web/src/App.tsx",
  action_at: "2026-10-17T09:59:55Z",
  turn_started_at: "2026-10-17T09:59:00Z",
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
  last_activity_at: "2026-10-17T09:59:55Z",
  ...over,
});

const overlaps = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe("layout", () => {
  const clusters = [cluster(), cluster({ key: "/r/api", label: "api", sessions: 4, shown: 2 })];
  const cards = [
    card(),
    card({ id: "s1:agent-a", parent: "s1", state: "running" }),
    card({ id: "s1:agent-b", parent: "s1" }),
    card({ id: "s2", session_id: "s2", cluster: "/r/api" }),
    card({ id: "s3", session_id: "s3", cluster: "/r/api" }),
  ];
  const lay = layout(["/r/app", "/r/api"], clusters, cards);

  test("frames don't overlap and subagents sit right of their parent", () => {
    const [a, b] = lay.frames;
    expect(overlaps(a, b)).toBe(false);
    expect(lay.cards["s1:agent-a"].x).toBeGreaterThan(lay.cards.s1.x + lay.cards.s1.w);
    expect(lay.cards["s1:agent-b"].y).toBeGreaterThan(lay.cards["s1:agent-a"].y);
    expect(overlaps(lay.cards.s2, lay.cards.s3)).toBe(false);
  });

  test("edges run parent to subagent; live ones are marked", () => {
    expect(lay.edges.map((e) => [e.from, e.to, e.live])).toEqual([
      ["s1", "s1:agent-a", true],
      ["s1", "s1:agent-b", false],
    ]);
  });

  test("a cluster with many sessions wraps into more columns", () => {
    const many = Array.from({ length: 12 }, (_, i) => card({ id: `m${i}`, session_id: `m${i}` }));
    const l = layout(["/r/app"], [cluster({ sessions: 12, shown: 12 })], many);
    const xs = new Set(many.map((c) => l.cards[c.id].x));
    expect(xs.size).toBeGreaterThan(1);
    expect(l.frames[0].h).toBeLessThan(1000);
    const rects = many.map((c) => l.cards[c.id]);
    expect(rects.every((r, i) => rects.every((q, j) => i === j || !overlaps(r, q)))).toBe(true);
  });

  test("a capped cluster gets a more slot", () => {
    expect(lay.frames[1].more?.count).toBe(2);
    expect(lay.frames[0].more).toBeNull();
  });

  test("cluster order stays stable as activity changes", () => {
    expect(stableOrder(["/r/app", "/r/api"], [cluster({ key: "/r/new" }), clusters[1], clusters[0]])).toEqual([
      "/r/app",
      "/r/api",
      "/r/new",
    ]);
    expect(stableOrder(["/r/gone", "/r/app"], [clusters[0]])).toEqual(["/r/app"]);
  });
});

describe("live state", () => {
  test("reported state wins; otherwise recent activity means working", () => {
    expect(liveState(card(), undefined, NOW)).toEqual({ state: "running", inferred: true });
    expect(liveState(card(), undefined, NOW + 120_000)).toEqual({ state: "idle", inferred: true });
    const s = { runtime_state: "waiting_for_input" } as never;
    expect(liveState(card(), s, NOW)).toEqual({ state: "waiting_for_input", inferred: false });
    expect(liveState(card({ state: "stopped", parent: "s0" }), undefined, NOW)).toEqual({ state: "stopped", inferred: true });
  });
});

describe("card", () => {
  const show = (c: MapCard, onOpen = vi.fn()) =>
    render(
      <Card card={c} rect={{ x: 0, y: 0, w: 248, h: 128 }} state="running" inferred now={NOW} selected={false} onOpen={onOpen} onFocus={() => {}} />,
    );

  test("shows progress as text, the state as words, and the latest file", () => {
    const onOpen = vi.fn();
    show(card(), onOpen);
    expect(screen.getByText("3 of 5 tasks · In progress")).toBeTruthy();
    expect(screen.getByText("Working")).toBeTruthy();
    expect(screen.getByRole("img", { name: "3 of 5 tasks" })).toBeTruthy();
    expect(screen.getByText(/App\.tsx \+3/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalled();
  });

  test("marks a session that skips permission prompts", () => {
    const { container, rerender } = show(card({ skip_permissions: true }));
    expect(container.querySelector(".rt .skip")?.textContent).toBe(" · skip permissions");
    rerender(
      <Card card={card()} rect={{ x: 0, y: 0, w: 248, h: 128 }} state="running" inferred now={NOW} selected={false} onOpen={() => {}} onFocus={() => {}} />,
    );
    expect(container.querySelector(".rt .skip")).toBeNull();
  });

  test("says when a runtime has no detail", () => {
    show(card({ runtime: "pi", detail: false, todos: null, files: 0, last_file: null }));
    expect(screen.getByText("No detail for this runtime")).toBeTruthy();
  });

  test("a new action floats its label above the card, then goes", () => {
    const { container, rerender } = show(card({ action_at: "2026-10-17T09:59:59Z" }));
    expect(container.querySelector(".map-ping")?.textContent).toBe("› Edit App.tsx");
    rerender(
      <Card card={card({ action_at: "2026-10-17T09:59:50Z" })} rect={{ x: 0, y: 0, w: 248, h: 128 }} state="running" inferred now={NOW} selected={false} onOpen={() => {}} onFocus={() => {}} />,
    );
    expect(container.querySelector(".map-ping")).toBeNull();
  });

  test("packets run along a wire only while its subagent works", () => {
    const edge = { from: "s1", to: "s1:agent-a", d: "M0 0 C10 0 10 10 20 10", live: true, cluster: "/r/app" };
    const { container, rerender } = render(<svg><Wire edge={edge} /></svg>);
    expect(container.querySelectorAll(".map-packet")).toHaveLength(3);
    expect(container.querySelector("mpath")?.getAttribute("href")).toBe("#wire-s1_agent-a");
    rerender(<svg><Wire edge={{ ...edge, live: false }} /></svg>);
    expect(container.querySelectorAll(".map-packet")).toHaveLength(0);
  });

  test("hostile titles render as text", () => {
    const { container } = show(card({ title: H, last_file: H, label: H }));
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });
});

describe("map view", () => {
  const snapshot: MapSnapshot = {
    now: {
      card_id: "s1",
      session_id: "s1",
      runtime: "claude",
      title: "Build the map",
      tool: "Edit",
      target: "web/src/App.tsx",
      action_at: "2026-10-17T09:59:55Z",
      turn_started_at: "2026-10-17T09:59:00Z",
      cluster: "/r/app",
    },
    clusters: [cluster()],
    cards: [card(), card({ id: "s1:agent-a", parent: "s1", label: "Explore", title: "Look around" })],
    pending_events: 0,
  };
  const activity: SessionActivity = {
    session_id: "s1",
    cluster: "/r/app",
    lanes: [
      {
        lane: "",
        title: "Build the map",
        label: null,
        current: { tool: "Edit", target: "web/src/App.tsx", at: "2026-10-17T09:59:55Z" },
        todos: [
          { id: "1", text: "Plan", status: "completed" },
          {
            id: "2",
            text: "Build",
            status: "in_progress",
            files: [
              { path: "web/src/App.tsx", op: "edit", at: "2026-10-17T09:59:55Z", n: 4 },
              { path: "web/src/api.ts", op: "edit", at: "2026-10-17T09:50:00Z" },
            ],
          },
        ],
        files: [
          { path: "web/src/App.tsx", op: "edit", at: "2026-10-17T09:59:55Z" },
          { path: "README.md", op: "write", at: "2026-10-17T09:50:00Z" },
        ],
        files_count: 2,
        last_activity_at: "2026-10-17T09:59:55Z",
      },
    ],
  };
  const replies: Record<string, unknown> = {
    "/api/v1/map": snapshot,
    "/api/v1/sessions/s1/activity": activity,
    "/api/v1/settings/watch": { enabled: true },
    "/api/v1/attention": { items: [{ attention_id: "a" }] },
  };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => new Response(JSON.stringify(replies[url.split("?")[0]] ?? {}), { status: 200 })),
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const mount = (over: Partial<Parameters<typeof MapView>[0]> = {}) =>
    render(
      <MapView
        ws={{ ready: true, feed: [], sessions: {}, attentionVersion: 0, streamState: "live" }}
        tabs={<span>tabs</span>}
        onOpenSession={() => {}}
        onOpenAttention={() => {}}
        {...over}
      />,
    );

  test("cards, the now card, folders, and the header status", async () => {
    const onOpenAttention = vi.fn();
    mount({ onOpenAttention });
    expect(await screen.findByText("Look around")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Now: claude Edit" })).toBeTruthy();
    expect(screen.getByText(/Working · 5s ago/)).toBeTruthy();
    expect(screen.getByText("watching")).toBeTruthy();
    fireEvent.click(await screen.findByText("1 needs you"));
    expect(onOpenAttention).toHaveBeenCalled();
    expect(screen.getByRole("navigation", { name: "Folders" }).textContent).toContain("app");
    expect(screen.getByText("~/r/app")).toBeTruthy();
  });

  const openCard = async () => {
    const card = (await screen.findAllByText("Build the map")).map((n) => n.closest("[data-card]")).find(Boolean) as HTMLElement;
    fireEvent.click(card);
    return { card, panel: await screen.findByRole("region", { name: "Tasks: Build the map" }) };
  };

  test("a card opens its tasks below it, with the files of the task in progress, and closes again", async () => {
    const onOpenSession = vi.fn();
    mount({ onOpenSession });
    const { card, panel } = await openCard();
    expect(card.getAttribute("aria-expanded")).toBe("true");
    expect(card.textContent).toContain("Hide tasks ↑");
    await waitFor(() => expect(panel.textContent).toContain("Plan"));
    expect(panel.textContent).toContain("Completed");
    expect(panel.textContent).toContain("Editing now"); // the session is working
    expect(panel.textContent).toContain("web/src/App.tsx ×4"); // in progress: open by default
    const plan = screen.getByRole("button", { name: /Plan/ });
    expect(plan.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(plan);
    expect(panel.textContent).toContain("No file edits");
    fireEvent.click(screen.getByText("Open in Sessions"));
    expect(onOpenSession).toHaveBeenCalledWith("s1");
    fireEvent.keyDown(panel, { key: "Escape" });
    await act(async () => {
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    expect(screen.queryByRole("region", { name: "Tasks: Build the map" })).toBeNull();
    expect(document.activeElement?.getAttribute("data-card")).toBe("s1");
    fireEvent.click(card);
    expect(await screen.findByRole("region", { name: "Tasks: Build the map" })).toBeTruthy();
    fireEvent.click(card); // a second click closes it
    expect(screen.queryByRole("region", { name: "Tasks: Build the map" })).toBeNull();
  });

  test("Stop asks first and is offered only when it works; Archive hides the session", async () => {
    const calls: { method: string; url: string; body: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ method: init?.method ?? "GET", url, body: (init?.body as string) ?? null });
        const path = url.split("?")[0];
        if (path.endsWith("/actions")) return new Response(JSON.stringify({ action: { state: "submitted" } }), { status: 202 });
        return new Response(JSON.stringify(replies[path] ?? { archived: true }), { status: 200 });
      }),
    );
    const managed = { session_id: "s1", archived: false, runtime: "claude", execution_cwd: "/r/app",
      controls: { stop: { enabled: true, reason: null } } } as never; // fmt: skip
    const onChanged = vi.fn();
    mount({ ws: { ready: true, feed: [], sessions: { s1: managed }, attentionVersion: 0, streamState: "live" }, onChanged });
    await openCard();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(calls.some((c) => c.url.endsWith("/actions"))).toBe(false); // cancelled
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/sessions/s1/actions"))?.body).toContain('"type":"stop"'));
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toContain('"archived":true'));
    expect(onChanged).toHaveBeenCalled();
    confirm.mockRestore();
  });

  test("Stop isn't offered for a session Robin can't control", async () => {
    mount();
    await openCard();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.getByRole("button", { name: "Archive" })).toBeTruthy();
  });

  test("zoom buttons change the level", async () => {
    mount();
    await screen.findByText("Look around");
    expect(screen.getByText("100%")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Zoom in"));
    expect(screen.getByText("120%")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Zoom out"));
    expect(screen.getByText("100%")).toBeTruthy();
  });

  test("an empty workspace says how to fill it", async () => {
    replies["/api/v1/map"] = { now: null, clusters: [], cards: [], pending_events: 0 };
    mount();
    expect(await screen.findByText("No sessions yet")).toBeTruthy();
    expect(screen.getByText("robin scan")).toBeTruthy();
    replies["/api/v1/map"] = snapshot;
  });
});
