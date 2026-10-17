import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { api, ApiError, type Session } from "../api";
import { ActivityFeed } from "../components/ActivityFeed";
import { SessionDetail } from "../components/SessionDetail";
import { GROUP, ROW, SessionList } from "../components/SessionList";
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
  title: null,
  cluster: "/work/app",
  last_activity_at: null,
  independence: null,
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  controls: {
    message: { enabled: true, reason: null },
    interrupt: { enabled: true, reason: null },
    stop: { enabled: true, reason: null },
    approval: { enabled: true, reason: null },
    handoff: { enabled: true, reason: null },
    reclaim: { enabled: false, reason: "Robin already controls this session" },
    takeover: { enabled: false, reason: "Robin already manages this session" },
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
    noise: false,
  };
  const { container } = render(<ActivityFeed items={[item]} scope={{}} sessions={{}} ready />);
  expect(await screen.findByText(/onerror/)).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
});

test("disabled controls say why", () => {
  const off = { enabled: false, reason: "session is disconnected" };
  render(<SessionDetail session={session({ connection_state: "disconnected",
    controls: { message: off, interrupt: off, stop: off, approval: off, handoff: off, reclaim: off, takeover: off } })} />);
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
      reclaim: { enabled: true, reason: null }, takeover: off } })} />);
  expect(screen.getByText("claude --resume abc-1")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Take back control" })).toHaveProperty("disabled", false);
  expect(screen.queryByRole("button", { name: "Hand off to terminal" })).toBeNull();
  expect(screen.getByRole("button", { name: "Interrupt" })).toHaveProperty("disabled", true);
});

const feedItem = (over: Partial<FeedItem>): FeedItem => ({
  global_cursor: 1, event_id: "e1", session_id: "s1", runtime: "claude", event_family: "message",
  event_type: "message.completed", evidence_kind: "reported", source_time: null,
  ingested_at: "2026-10-15T00:00:00Z", payload_ref: null, task_id: null, preview: null, noise: false, ...over,
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ events: [], cards: [] }), { status: 200 })));
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
  fireEvent.click(await screen.findByLabelText("Show all events"));
  expect((await screen.findAllByText("session.state_changed")).length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText("Evidence"), { target: { value: "reported" } });
  expect(screen.queryAllByText("session.state_changed")).toHaveLength(0);
  expect(screen.getAllByText("message.completed").length).toBeGreaterThan(0);
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

test("an independent review says whether it is isolated", () => {
  const { rerender } = render(<SessionDetail session={session({ independence: "enforced" })} />);
  expect(screen.getByText("Independent: can't read other agents' findings")).toBeTruthy();
  rerender(<SessionDetail session={session({ runtime: "pi", independence: "not_isolated" })} />);
  expect(screen.getByText(/Not isolated/)).toBeTruthy();
});

test("a long session list renders only the rows in view, and scrolling moves the window", () => {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(640);
  const many = Array.from({ length: 5000 }, (_, i) =>
    session({ session_id: `s${i}`, title: `task ${i}`, execution_cwd: `/work/app${i}`, mode: "observed", branch: null }),
  );
  const onSelect = vi.fn();
  const { container, rerender } = render(<SessionList sessions={many} selected={null} onSelect={onSelect} />);
  expect(container.querySelectorAll("li").length).toBeLessThan(40);
  expect(screen.getByText("task 0")).toBeTruthy();
  expect(screen.queryByText("task 2600")).toBeNull();
  rerender(<SessionList sessions={many} selected="s2600" onSelect={onSelect} />);

  fireEvent.scroll(container.querySelector(".session-scroll")!, { target: { scrollTop: GROUP + 2600 * ROW } });
  expect(screen.queryByText("task 0")).toBeNull();
  const row = screen.getByText("task 2600").closest("button")!;
  expect(row.getAttribute("aria-current")).toBe("true");
  fireEvent.click(row);
  expect(onSelect).toHaveBeenCalledWith("s2600");
  vi.restoreAllMocks();
});

test("the session list is one tab stop and the keyboard reaches every session", () => {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(640);
  const many = Array.from({ length: 200 }, (_, i) => session({ session_id: `s${i}`, execution_cwd: `/w/app${i}`, branch: null }));
  const { container } = render(<SessionList sessions={many} selected={null} onSelect={() => {}} />);
  expect(container.querySelectorAll('button[tabindex="0"]')).toHaveLength(1);
  const first = container.querySelector<HTMLElement>('button[tabindex="0"]')!;
  first.focus();
  fireEvent.keyDown(first, { key: "End" });
  expect(document.activeElement?.textContent).toContain("app199");
  fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
  expect(document.activeElement?.textContent).toContain("app198");
  fireEvent.keyDown(document.activeElement!, { key: "Home" });
  expect(document.activeElement?.textContent).toContain("app0");
  expect(container.querySelector("li[aria-setsize]")!.getAttribute("aria-setsize")).toBe("200");
  vi.restoreAllMocks();
});

test("a session state change is announced", () => {
  const { rerender } = render(<SessionDetail session={session()} />);
  rerender(<SessionDetail session={session({ runtime_state: "waiting_for_input" })} />);
  expect(screen.getAllByRole("status").some((n) => /claude Needs input/.test(n.textContent ?? ""))).toBe(true);
});

test("a disabled control's reason is shown as text, not only a tooltip", () => {
  const s = session();
  render(<SessionDetail session={{ ...s, controls: { ...s.controls, stop: { enabled: false, reason: "Already stopping" } } }} />);
  expect(screen.getByText("Already stopping")).toBeTruthy();
});

test("the session list groups by folder and shows each session's title, state and age", () => {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(640);
  const recent = new Date(Date.now() - 5_000).toISOString();
  const list = [
    session({ session_id: "a", title: "Fix the login test", cluster: "/r/app", runtime_state: "unknown", mode: "observed", last_activity_at: recent }),
    session({ session_id: "b", title: null, cluster: "/r/api", runtime: "codex", runtime_state: "idle" }),
    session({ session_id: "c", title: "Ship it", cluster: "/r/app", runtime_state: "waiting_for_input" }),
  ];
  const { container } = render(<SessionList sessions={list} selected={null} onSelect={() => {}} />);
  const rows = [...container.querySelectorAll(".session-group, .session-item .title")].map((n) => n.textContent);
  expect(rows).toEqual(["app2", "Fix the login test", "Ship it", "api1", "codex · b"]);
  const inferred = screen.getByText("Fix the login test").closest("button")!;
  expect(inferred.textContent).toContain("Working");
  expect(inferred.textContent).toContain("guess");
  expect(inferred.querySelector("[title='Inferred from recent activity']")).toBeTruthy();
  expect(screen.getByText("Ship it").closest("button")!.textContent).toContain("Needs input");
  vi.restoreAllMocks();
});

test("a session started outside Robin is read-only, with its resume command and Archive", () => {
  render(<SessionDetail session={session({ mode: "observed", native_session_id: "abc-1", title: "Review it" })} />);
  expect(screen.getByRole("heading", { name: /Review it/ })).toBeTruthy();
  expect(screen.getByText(/Read-only here/)).toBeTruthy();
  expect(screen.getByText("claude --resume abc-1")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Archive" })).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Interrupt" })).toBeNull();
});

const continuable = (s: Session): Session => ({
  ...s,
  controls: { ...s.controls, takeover: { enabled: true, reason: null } },
});

test("a session started outside Robin can be continued by sending it a message", async () => {
  const send = vi.spyOn(api, "act").mockResolvedValue({
    action: { action_id: "a1", session_id: "s1", type: "takeover", state: "submitted", error: null },
  });
  const onChanged = vi.fn();
  render(<SessionDetail session={continuable(session({ mode: "observed", native_session_id: "abc-1" }))} onChanged={onChanged} />);
  expect(screen.getByText("Sending continues this session in Robin.")).toBeTruthy();
  expect(screen.queryByText(/Read-only here/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Interrupt" })).toBeNull();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "keep going" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Send: submitted")).toBeTruthy();
  expect(send).toHaveBeenCalledWith("s1", { type: "takeover", text: "keep going" });
  expect(onChanged).toHaveBeenCalled();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
});

test("a refused takeover says why and keeps the message", async () => {
  vi.spyOn(api, "act").mockRejectedValue(
    new ApiError(409, "terminal_still_open", "close it in your terminal first (process 42)", "reload_state"),
  );
  render(<SessionDetail session={continuable(session({ mode: "observed", native_session_id: "abc-1" }))} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "keep going" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Send not sent: close it in your terminal first (process 42)")).toBeTruthy();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("keep going");
});

test("a session that can't be continued stays read-only and says why", () => {
  const s = session({ mode: "observed", native_session_id: "abc-1", runtime: "mystery" });
  render(<SessionDetail session={{ ...s, controls: { ...s.controls, takeover: { enabled: false, reason: "no Robin adapter for mystery" } } }} />);
  expect(screen.getByText(/Read-only here/)).toBeTruthy();
  expect(screen.getByText(/Can't continue it here: no Robin adapter for mystery/)).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
});

test("a managed session that can't take messages hides the box but keeps its controls", () => {
  const s = session({ connection_state: "disconnected" });
  render(<SessionDetail session={{ ...s, controls: { ...s.controls, message: { enabled: false, reason: "session is disconnected" } } }} />);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Interrupt" })).toBeTruthy();
  expect(screen.getByText(/Controls unavailable: session is disconnected/)).toBeTruthy();
});

test("the timeline hides noise until asked and groups a session's events into turns", async () => {
  const items = [
    feedItem({ event_id: "p2", global_cursor: 6, event_type: "message.user", preview: "You: make it violet" }),
    feedItem({ event_id: "n", global_cursor: 5, event_type: "unknown.raw", event_family: "unknown", preview: null, noise: true }),
    feedItem({ event_id: "d", global_cursor: 4, event_type: "session.turn_completed", event_family: "session", preview: "Turn completed" }),
    feedItem({ event_id: "t", global_cursor: 3, event_type: "tool.started", event_family: "tool", preview: "Bash npm test" }),
    feedItem({ event_id: "p1", global_cursor: 2, event_type: "message.user", preview: "You: fix the test" }),
  ];
  const { container } = render(<ActivityFeed items={items} scope={{ session_id: "s1" }} sessions={{ s1: session() }} ready />);
  await screen.findByText("You: make it violet");
  const turns = [...container.querySelectorAll(".feed-turn > h3 span")].map((n) => n.textContent);
  expect(turns).toEqual(["You: make it violet", "You: fix the test"]); // newest turn first
  const first = container.querySelectorAll(".feed-turn")[1];
  expect([...first.querySelectorAll(".feed-item .preview")].map((n) => n.textContent)).toEqual(["Bash npm test", "Turn completed"]);
  expect(screen.queryByText("unknown.raw")).toBeNull();
  fireEvent.click(screen.getByLabelText("Show all events"));
  expect(screen.getAllByText("unknown.raw").length).toBeGreaterThan(0);
});

test("session titles with markup render as text", () => {
  const H = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const s = session({ title: H });
  const { container } = render(
    <>
      <SessionList sessions={[s]} selected={null} onSelect={() => {}} />
      <SessionDetail session={s} />
    </>,
  );
  expect(container.querySelector("img, script")).toBeNull();
  expect(container.textContent).toContain("<script>");
});
