import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { MapCard, MapCluster } from "../api";
import { MapView } from "../map/MapView";
import { inRecency, type Recency } from "../recency";

const NOW = Date.parse("2026-10-17T10:00:00Z");
const ago = (h: number) => new Date(NOW - h * 3_600_000).toISOString();

describe("inRecency", () => {
  test("working and waiting are always active, whatever their age", () => {
    expect(inRecency("running", ago(500), NOW, "active")).toBe(true);
    expect(inRecency("waiting_for_input", null, NOW, "active")).toBe(true);
  });
  test("active is only working or waiting; idle counts for a day as today", () => {
    expect(inRecency("idle", ago(0), NOW, "active")).toBe(false);
    expect(inRecency("stopped", ago(0), NOW, "active")).toBe(false);
    expect(inRecency("idle", ago(3), NOW, "today")).toBe(true);
    expect(inRecency("idle", ago(30), NOW, "today")).toBe(false);
    expect(inRecency("idle", ago(30), NOW, "all")).toBe(true);
    expect(inRecency("stopped", null, NOW, "today")).toBe(false);
  });
  test("a failure is not active and counts for a day as today", () => {
    expect(inRecency("failed", ago(0), NOW, "active")).toBe(false);
    expect(inRecency("failed", ago(10), NOW, "today")).toBe(true);
    expect(inRecency("failed", ago(30), NOW, "today")).toBe(false);
  });
});

describe("Map recency", () => {
  const card = (id: string, cluster: string, hours: number, over: Partial<MapCard> = {}): MapCard => ({
    id,
    session_id: id,
    parent: null,
    cluster,
    runtime: "claude",
    mode: "observed",
    label: null,
    title: `Session ${id}`,
    state: "idle",
    state_evidence: "reported",
    detail: false,
    todos: null,
    files: 0,
    last_file: null,
    tool: null,
    target: null,
    action_at: null,
    turn_started_at: null,
    last_activity_at: ago(hours),
    created_at: null,
    subagents_hidden: 0,
    ...over,
  });
  const cluster = (key: string, sessions: number, shown: number): MapCluster => ({
    key,
    label: key.split("/").pop()!,
    path: `~${key}`,
    sessions,
    shown,
    running: 0,
    waiting: 0,
    last_activity_at: null,
  });
  const snapshot = {
    now: null,
    pending_events: 0,
    clusters: [cluster("/r/app", 9, 2), cluster("/r/legacy", 1, 1)],
    cards: [
      card("fresh", "/r/app", 0.5, { state: "running" }),
      card("idle", "/r/app", 0.5),
      card("fresh:agent", "/r/app", 0.5, { session_id: "fresh", parent: "fresh" }),
      card("stale", "/r/app", 5),
      card("ancient", "/r/legacy", 100),
    ],
  };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/map") ? snapshot : {}), { status: 200 })),
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const mount = (recency: Recency, onRecency = vi.fn()) =>
    render(
      <MapView
        ws={{ ready: true, feed: [], sessions: {}, attentionVersion: 0, streamState: "live" }}
        tabs={null}
        recency={recency}
        onRecency={onRecency}
        onOpenSession={() => {}}
        onOpenAttention={() => {}}
      />,
    );

  test("Active shows working sessions with their subagents, drops empty folders and the +more", async () => {
    const onRecency = vi.fn();
    mount("active", onRecency);
    expect((await screen.findAllByText("Session fresh")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Session stale")).toBeNull();
    expect(screen.queryByText("Session idle")).toBeNull(); // just finished is Today, not Active
    expect(document.querySelector('[data-card="fresh:agent"]')).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Folders" }).textContent).not.toContain("legacy");
    expect(screen.queryByText(/more$/)).toBeNull();
    expect(screen.getByRole("button", { name: "Active 1" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Today 3" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "All 10" }));
    expect(onRecency).toHaveBeenCalledWith("all");
  });

  test("an empty Active view offers today's sessions", async () => {
    snapshot.cards = [card("ancient", "/r/legacy", 100)];
    const onRecency = vi.fn();
    mount("active", onRecency);
    fireEvent.click(await screen.findByRole("button", { name: "Show today's sessions" }));
    expect(onRecency).toHaveBeenCalledWith("today");
  });
});
