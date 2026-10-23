import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { AttentionItem, MapCard, Session } from "../api";
import { PhoneMap } from "../phone/PhoneMap";
import { PhoneShell } from "../phone/PhoneShell";
import { PHONE_QUERY, useIsPhone } from "../useMediaQuery";

const NOW = Date.parse("2026-10-17T10:00:00Z");

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
const folder = (key: string, label: string) => ({
  key,
  label,
  path: `~${key}`,
  sessions: 1,
  shown: 1,
  running: 0,
  waiting: 0,
  last_activity_at: null,
});
const question: AttentionItem = {
  attention_id: "att-2",
  kind: "question",
  session_id: "s1",
  task_id: null,
  state: "open",
  title: "Create the branch?",
  detail: {
    tool: "AskUserQuestion",
    answer_in: "robin",
    questions: [{ question: "Create the branch?", header: "", multi: false, options: [{ label: "Yes, and fix", description: "" }] }],
  },
  allowed_responses: ["answer", "deny"],
  created_at: "2026-10-17T09:59:30Z",
};
const snapshot = {
  now: null,
  pending_events: 0,
  clusters: [folder("/r/app", "app"), folder("/r/site", "site")],
  cards: [
    card(),
    card({ id: "ship", parent: "s1", kind: "skill", title: "Ship", label: "Skill", inner: ["review-lite", "qa"] }),
    card({ id: "t1", parent: "s1", kind: "subagent", title: "Verify suite", status: "completed" }),
    card({ id: "wf", parent: "s1", kind: "workflow", title: "Review-pipeline", status: "completed", phases: [{ name: "Find", done: 3, failed: 0, total: 3 }] }),
    card({ id: "wf:a", parent: "wf", kind: "subagent", title: "Find:security", phase: "Find" }),
    card({ id: "s9", session_id: "s9", cluster: "/r/site", title: "Hero copy pass" }),
  ],
};
let replies: Record<string, unknown>;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  replies = {
    "/api/v1/map": snapshot,
    "/api/v1/attention": { items: [question] },
    "/api/v1/sessions/s1/activity": { session_id: "s1", cluster: "/r/app", lanes: [] },
    "/api/v1/adapters": { adapters: [] },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => new Response(JSON.stringify(replies[url.split("?")[0]] ?? {}), { status: 200 })),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("phone detection", () => {
  test("false without matchMedia; follows the query as it changes", () => {
    expect(renderHook(() => useIsPhone()).result.current).toBe(false);
    let narrow = true;
    const listeners: (() => void)[] = [];
    vi.stubGlobal("matchMedia", (q: string) => ({
      get matches() {
        return q === PHONE_QUERY && narrow;
      },
      addEventListener: (_: string, l: () => void) => listeners.push(l),
      removeEventListener: () => {},
    }));
    const { result } = renderHook(() => useIsPhone());
    expect(result.current).toBe(true);
    narrow = false;
    act(() => listeners.forEach((l) => l()));
    expect(result.current).toBe(false);
  });
});

describe("phone map", () => {
  const mount = () =>
    render(
      <PhoneMap
        ws={{ ready: true, feed: [], sessions: {}, attentionVersion: 0 }}
        recency="all"
        onRecency={() => {}}
        onOpenSession={() => {}}
      />,
    );

  test("one folder at a time, the one that needs you first, with its count", async () => {
    mount();
    const tabs = await screen.findByRole("tablist", { name: "Folders" });
    await waitFor(() => expect(screen.getByRole("tab", { name: /app/ }).textContent).toContain("● 1"));
    expect(tabs.textContent).toContain("site");
    expect(screen.getByRole("tab", { name: /app/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("Ship")).toBeTruthy();
    expect(screen.queryByText("Hero copy pass")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /site/ }));
    expect(screen.getByText("Hero copy pass")).toBeTruthy();
    expect(screen.queryByText("Ship")).toBeNull();
  });

  test("finished children fold into a pill that opens and closes; a workflow counts its agents", async () => {
    mount();
    const pill = await screen.findByRole("button", { name: "✓ 2 done ▾" });
    expect(screen.queryByText("Verify suite")).toBeNull();
    fireEvent.click(pill);
    expect(screen.getByText("Verify suite")).toBeTruthy();
    expect(screen.getByText("3 agents ›")).toBeTruthy();
    expect(screen.queryByText("Find:security")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "✓ 2 done ▴" }));
    expect(screen.queryByText("Verify suite")).toBeNull();
  });

  test("a card waiting on you opens its question in a sheet", async () => {
    mount();
    const chip = await screen.findByText("Needs an answer");
    fireEvent.click(chip.closest("[data-card]") as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    expect(sheet.textContent).toContain("Yes, and fix");
    expect(sheet.querySelector("button[type=submit]")).not.toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("phone shell", () => {
  const session = {
    session_id: "s1",
    runtime: "claude",
    title: "Check the ticket",
    updated_at: "2026-10-17T09:59:55Z",
    last_activity_at: "2026-10-17T09:59:55Z",
    runtime_state: "idle",
    mode: "managed",
    archived: false,
    execution_cwd: "/r/app",
    shared_folder_with: [],
  } as unknown as Session;
  const ws = {
    ready: true,
    feed: [],
    sessions: { s1: session },
    attentionVersion: 0,
    streamState: "live",
    reload: async () => {},
  } as unknown as Parameters<typeof PhoneShell>[0]["ws"];
  const base = {
    ws,
    sessions: [session],
    filtered: 1,
    counts: { active: 1, today: 1, all: 1 },
    recency: "all" as const,
    onRecency: () => {},
    writers: [],
    theme: "system" as const,
    onTheme: () => {},
    onStorageChanged: () => {},
    notice: null,
    onOpenSession: () => {},
    onLaunched: () => {},
  };

  test("four tabs, Inbox first with how many are waiting", async () => {
    render(<PhoneShell {...base} tab="inbox" onTab={() => {}} selected={undefined} onSelect={() => {}} detail={null} />);
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect(nav.textContent).toMatch(/Inbox.*Sessions.*Map.*Settings/);
    expect(screen.getByRole("button", { name: /Inbox/ }).getAttribute("aria-current")).toBe("page");
    expect(await screen.findByLabelText("1 waiting")).toBeTruthy();
    expect((await screen.findAllByText("Create the branch?")).length).toBeGreaterThan(0);
  });

  test("a session opens full screen and Back returns to the list", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <PhoneShell {...base} tab="sessions" onTab={() => {}} selected={undefined} onSelect={onSelect} detail={null} />,
    );
    fireEvent.click(screen.getByText("Check the ticket"));
    expect(onSelect).toHaveBeenCalledWith("s1");
    rerender(
      <PhoneShell {...base} tab="sessions" onTab={() => {}} selected={session} onSelect={onSelect} detail={<p>the detail</p>} />,
    );
    expect(screen.getByText("the detail")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Show sessions" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  test("Launch opens as a sheet and Escape closes it, back on the button", async () => {
    render(<PhoneShell {...base} tab="sessions" onTab={() => {}} selected={undefined} onSelect={() => {}} detail={null} />);
    const add = screen.getByRole("button", { name: "Launch session" });
    add.focus();
    fireEvent.click(add);
    expect(screen.getByRole("dialog", { name: "Launch a session" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(add));
  });
});
