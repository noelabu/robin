import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { api, ApiError, type ContinuePreview, type Session } from "../api";
import { ContinueDialog } from "../components/ContinueDialog";
import { SessionDetail } from "../components/SessionDetail";

const session = (over: Partial<Session> = {}): Session => ({
  session_id: "s1",
  runtime: "claude",
  native_session_id: "n1",
  mode: "observed",
  runtime_state: "idle",
  connection_state: "disconnected",
  state_evidence_at: null,
  active_run_id: null,
  execution_cwd: "/work/app",
  lease_generation: 0,
  control_owner: null,
  project_id: null,
  current_task_id: null,
  archived: false,
  branch: "main",
  shared_folder_with: [],
  title: null,
  cluster: "/work/app",
  last_activity_at: null,
  independence: null,
  skip_permissions: false,
  continued_from: null,
  continued_in: [],
  created_at: "2026-10-18T00:00:00Z",
  updated_at: "2026-10-18T00:00:00Z",
  controls: {
    message: { enabled: false, reason: "observed sessions are read-only in Robin" },
    interrupt: { enabled: false, reason: null },
    stop: { enabled: false, reason: null },
    approval: { enabled: false, reason: null },
    handoff: { enabled: false, reason: null },
    reclaim: { enabled: false, reason: null },
    takeover: { enabled: true, reason: null },
  },
  capabilities: { runtime: "claude", steer_mid_turn: false, tested_versions: "x", level: "live" },
  ...over,
});

const preview = (over: Partial<ContinuePreview> = {}): ContinuePreview => ({
  source: { session_id: "s1", runtime: "claude", native_session_id: "n1", mode: "observed", cwd: "/work/app" },
  target: { runtime: "codex", model: "gpt-6-astra", context_window: 272000, window_source: "models_cache.json" },
  messages: { total: 184, user: 41, assistant: 77, tool_calls: 33, tool_results: 33 },
  dropped: { images: 2, sidechain_records: 17, thinking_as_text: 12, tool_calls_as_text: 33 },
  compaction: { source_summary_used: true },
  estimate: { bytes: 412000, tokens: 103000, limit_tokens: 163200, fits: true },
  inherits: { task_id: "t1", project_id: null, skip_permissions: true, independence: null },
  warnings: [],
  refusal: null,
  default_text: "Continue from where the previous session left off.",
  preview_hash: "sha256:abc",
  ...over,
});

const action = (state: string) => ({ action_id: "a1", session_id: "s2", type: "takeover", state, error: null }) as never;

beforeEach(() => vi.restoreAllMocks());

test("offers the other runtimes, and nothing for a session without a native id", () => {
  const { unmount } = render(<ContinueDialog session={session()} />);
  expect(screen.getByRole("button", { name: "Codex" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Pi" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Claude Code" })).toBeNull();
  unmount();
  const { container } = render(<ContinueDialog session={session({ native_session_id: null })} />);
  expect(container.textContent).toBe("");
});

test("is disabled while a turn is running", () => {
  render(<ContinueDialog session={session({ mode: "managed", runtime_state: "running" })} />);
  const menu = screen.getByText("Continue in…");
  expect(menu.getAttribute("aria-disabled")).toBe("true");
  expect(menu.getAttribute("title")).toMatch(/turn is running/);
});

test("previews what crosses, then continues with the edited first message and opens the new session", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(preview());
  const go = vi.spyOn(api, "continueIn").mockResolvedValue({ action: action("submitted"), session_id: "s2" });
  vi.spyOn(api, "action").mockResolvedValue(action("acknowledged"));
  const open = vi.fn();
  render(<ContinueDialog session={session()} onOpenSession={open} />);
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  expect(await screen.findByRole("heading", { name: "Continue in Codex" })).toBeTruthy();
  expect(screen.getByText(/184 \(41 from you, 77 replies\) · 33 tool calls and 12 thinking blocks as text/)).toBeTruthy();
  expect(screen.getByText(/2 images → \[image\], 17 subagent records/)).toBeTruthy();
  expect(screen.getByText(/~103k of 163k allowed/)).toBeTruthy();
  expect(screen.getByText(/its task · permission prompts skipped/)).toBeTruthy();
  const box = screen.getByLabelText("First message") as HTMLTextAreaElement;
  expect(box.value).toBe("Continue from where the previous session left off.");
  fireEvent.change(box, { target: { value: "Now write the tests" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await waitFor(() => expect(open).toHaveBeenCalledWith("s2"));
  expect(go).toHaveBeenCalledWith("s1", "codex", "Now write the tests", "sha256:abc");
});

test("a refused preview says why and offers no Continue", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(
    preview({ refusal: { code: "too_large", message: "~410k tokens is over the 163k limit" } }),
  );
  render(<ContinueDialog session={session()} />);
  fireEvent.click(screen.getByRole("button", { name: "Pi" }));
  expect((await screen.findByRole("alert")).textContent).toContain("over the 163k limit");
  expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview again" })).toBeTruthy();
});

test("a stale preview asks for a new one", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(preview());
  vi.spyOn(api, "continueIn").mockRejectedValue(new ApiError(409, "preview_stale", "changed", "reload_state"));
  render(<ContinueDialog session={session()} />);
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  expect((await screen.findByRole("alert")).textContent).toBe("The session changed since the preview.");
  expect(screen.getByRole("button", { name: "Preview again" })).toBeTruthy();
});

test("a target that doesn't start points at the converted session it kept", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(preview());
  vi.spyOn(api, "continueIn").mockResolvedValue({ action: action("failed"), session_id: "s2" });
  const open = vi.fn();
  render(<ContinueDialog session={session()} onOpenSession={open} />);
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(/Codex didn't start/);
  fireEvent.click(screen.getByRole("button", { name: "Open the converted session" }));
  expect(open).toHaveBeenCalledWith("s2");
});

test("a start that never finishes stops waiting and points at the converted session", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(preview());
  vi.spyOn(api, "continueIn").mockResolvedValue({ action: action("submitted"), session_id: "s2" });
  const poll = vi.spyOn(api, "action").mockResolvedValue(action("submitted"));
  const open = vi.fn();
  render(<ContinueDialog session={session()} onOpenSession={open} />);
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  const go = await screen.findByRole("button", { name: "Continue" });
  let now = 1_000;
  vi.spyOn(Date, "now").mockImplementation(() => (now += 61_000));
  fireEvent.click(go);
  expect((await screen.findByRole("alert")).textContent).toMatch(/Codex is taking too long to start/);
  expect(screen.getByRole("button", { name: "Open the converted session" })).toBeTruthy();
  expect(poll).not.toHaveBeenCalled();
  expect(open).not.toHaveBeenCalled();
});

test("leaving the session while it starts doesn't pull the user back", async () => {
  vi.spyOn(api, "continuePreview").mockResolvedValue(preview());
  vi.spyOn(api, "continueIn").mockResolvedValue({ action: action("submitted"), session_id: "s2" });
  let finish: (a: never) => void = () => {};
  const poll = vi.spyOn(api, "action").mockReturnValue(new Promise((res) => (finish = res)));
  const open = vi.fn();
  const { unmount } = render(<ContinueDialog session={session()} onOpenSession={open} />);
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
  await waitFor(() => expect(poll).toHaveBeenCalled());
  unmount();
  finish(action("acknowledged"));
  await new Promise((res) => setTimeout(res, 50));
  expect(open).not.toHaveBeenCalled();
});

test("the session header links both ways", () => {
  vi.spyOn(api, "conversation").mockResolvedValue({ messages: [] });
  vi.spyOn(api, "mapCards").mockReturnValue(new Promise(() => {})); // the flow panel isn't under test
  const open = vi.fn();
  render(
    <SessionDetail
      session={session({
        continued_from: { session_id: "s0", runtime: "pi", at: "x" },
        continued_in: [{ session_id: "s2", runtime: "codex", at: "y" }],
      })}
      onOpenSession={open}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "← continued from pi" }));
  fireEvent.click(screen.getByRole("button", { name: "continued in codex →" }));
  expect(open.mock.calls).toEqual([["s0"], ["s2"]]);
});
