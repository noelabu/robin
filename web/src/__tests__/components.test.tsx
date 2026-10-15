import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import type { Session } from "../api";
import { ActivityFeed } from "../components/ActivityFeed";
import { SessionDetail } from "../components/SessionDetail";
import type { FeedItem } from "../useWorkspace";

const session = (over: Partial<Session> = {}): Session => ({
  session_id: "s1",
  runtime: "claude",
  native_session_id: null,
  mode: "managed",
  runtime_state: "running",
  connection_state: "connected",
  state_evidence_at: null,
  active_run_id: null,
  execution_cwd: "/work/app",
  lease_generation: 0,
  control_owner: "robin",
  project_id: null,
  current_task_id: null,
  archived: false,
  branch: "main",
  shared_folder_with: [],
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  controls: {
    message: { enabled: true, reason: null },
    interrupt: { enabled: true, reason: null },
    stop: { enabled: true, reason: null },
    approval: { enabled: true, reason: null },
    handoff: { enabled: true, reason: null },
    reclaim: { enabled: false, reason: "Robin already controls this session" },
  },
  capabilities: { runtime: "claude", steer_mid_turn: false, tested_versions: "x", level: "live" },
  ...over,
});

test("agent text renders as text, never as markup", async () => {
  const item: FeedItem = {
    global_cursor: 1, event_id: "e1", session_id: "s1", runtime: "claude", event_family: "message",
    event_type: "message.completed", evidence_kind: "reported", source_time: null,
    ingested_at: "2026-10-15T00:00:00Z", payload_ref: null, task_id: null,
    preview: '<img src=x onerror="alert(1)"><script>alert(2)</script>',
  };
  const { container } = render(<ActivityFeed items={[item]} scope={{}} sessions={{}} ready />);
  expect(await screen.findByText(/onerror/)).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
});

test("disabled controls say why", () => {
  const off = { enabled: false, reason: "session is disconnected" };
  render(<SessionDetail session={session({ connection_state: "disconnected",
    controls: { message: off, interrupt: off, stop: off, approval: off, handoff: off, reclaim: off } })} />);
  expect(screen.getByRole("button", { name: "Interrupt" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/Controls unavailable: session is disconnected/)).toBeTruthy();
});

test("input during a turn is labelled by what the runtime does with it", () => {
  render(<SessionDetail session={session()} />);
  expect(screen.getByRole("button", { name: "Queue follow-up" })).toBeTruthy();
  expect(screen.getByText(/applies input sent during a turn after the turn ends/)).toBeTruthy();
});

test("a runtime that steers mid-turn says Steer", () => {
  render(<SessionDetail session={session({ runtime: "codex",
    capabilities: { runtime: "codex", steer_mid_turn: true, tested_versions: "x", level: "live" } })} />);
  expect(screen.getByRole("button", { name: "Steer" })).toBeTruthy();
});

test("a handed-off session shows the resume command and only Take back control", () => {
  const off = { enabled: false, reason: "handed off to a terminal; take control back first" };
  render(<SessionDetail session={session({ control_owner: "terminal", native_session_id: "abc-1",
    runtime_state: "idle", connection_state: "disconnected",
    controls: { message: off, interrupt: off, stop: off, approval: off, handoff: off,
      reclaim: { enabled: true, reason: null } } })} />);
  expect(screen.getByText("claude --resume abc-1")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Take back control" })).toHaveProperty("disabled", false);
  expect(screen.queryByRole("button", { name: "Hand off to terminal" })).toBeNull();
  expect(screen.getByRole("button", { name: "Interrupt" })).toHaveProperty("disabled", true);
});

const feedItem = (over: Partial<FeedItem>): FeedItem => ({
  global_cursor: 1, event_id: "e1", session_id: "s1", runtime: "claude", event_family: "message",
  event_type: "message.completed", evidence_kind: "reported", source_time: null,
  ingested_at: "2026-10-15T00:00:00Z", payload_ref: null, task_id: null, preview: null, ...over,
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ events: [] }), { status: 200 })));
});

test("an empty feed says whether nothing happened or Robin couldn't see", async () => {
  const s = session({ connection_state: "disconnected" });
  render(<ActivityFeed items={[]} scope={{ session_id: "s1" }} sessions={{ s1: s }} ready />);
  expect(await screen.findByText(/Robin is disconnected from this session, so activity may be missing/)).toBeTruthy();
});

test("a connected session with no events says so plainly", async () => {
  render(<ActivityFeed items={[]} scope={{ session_id: "s1" }} sessions={{ s1: session() }} ready />);
  expect(await screen.findByText(/No activity yet. Robin is connected/)).toBeTruthy();
});

test("evidence kinds are labelled separately and can be filtered", async () => {
  const items = [
    feedItem({ event_id: "a", global_cursor: 2, evidence_kind: "reported", event_type: "message.completed" }),
    feedItem({ event_id: "b", global_cursor: 1, evidence_kind: "inferred", event_type: "session.state_changed" }),
  ];
  render(<ActivityFeed items={items} scope={{}} sessions={{}} ready />);
  expect(await screen.findByText("session.state_changed")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Evidence"), { target: { value: "reported" } });
  expect(screen.queryByText("session.state_changed")).toBeNull();
  expect(screen.getByText("message.completed")).toBeTruthy();
});

test("a session sharing its folder warns about collisions and shows its branch", () => {
  render(<SessionDetail session={session({ shared_folder_with: ["s2"] })} />);
  expect(screen.getByRole("alert").textContent).toMatch(/Another session may be editing this folder/);
  expect(screen.getByText("main")).toBeTruthy();
});

test("archive is offered, and restore once archived", () => {
  const { rerender } = render(<SessionDetail session={session()} />);
  expect(screen.getByRole("button", { name: "Archive" })).toBeTruthy();
  rerender(<SessionDetail session={session({ archived: true })} />);
  expect(screen.getByRole("button", { name: "Restore" })).toBeTruthy();
});
