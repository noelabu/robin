import { render, screen } from "@testing-library/react";
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
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  controls: {
    message: { enabled: true, reason: null },
    interrupt: { enabled: true, reason: null },
    stop: { enabled: true, reason: null },
    approval: { enabled: true, reason: null },
  },
  capabilities: { runtime: "claude", steer_mid_turn: false, tested_versions: "x", level: "live" },
  ...over,
});

test("agent text renders as text, never as markup", () => {
  const item: FeedItem = {
    global_cursor: 1, event_id: "e1", session_id: "s1", runtime: "claude", event_family: "message",
    event_type: "message.completed", evidence_kind: "reported", source_time: null,
    ingested_at: "2026-10-15T00:00:00Z", payload_ref: null,
    preview: '<img src=x onerror="alert(1)"><script>alert(2)</script>',
  };
  const { container } = render(<ActivityFeed items={[item]} sessionId={null} ready />);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText(/onerror/)).toBeTruthy();
});

test("disabled controls say why", () => {
  const off = { enabled: false, reason: "session is disconnected" };
  render(<SessionDetail session={session({ connection_state: "disconnected",
    controls: { message: off, interrupt: off, stop: off, approval: off } })} />);
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
