import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { AttentionItem, MapCard } from "../api";
import { QuestionList } from "../components/Approval";
import { MapView } from "../map/MapView";

const NOW = Date.parse("2026-10-17T10:00:00Z");

const card: MapCard = {
  id: "s1",
  session_id: "s1",
  parent: null,
  cluster: "/r/app",
  runtime: "claude",
  mode: "managed",
  label: null,
  title: "Ship the release",
  state: "waiting_for_input",
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
};
const approval: AttentionItem = {
  attention_id: "att-1",
  kind: "approval",
  session_id: "s1",
  task_id: null,
  state: "open",
  title: "Bash needs approval",
  detail: { tool: "Bash", input: { command: "git push" } },
  allowed_responses: ["allow", "deny"],
  created_at: "2026-10-17T09:59:30Z",
};

test("a session waiting on a permission turns its card red, and the card answers it", async () => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  const calls: { method: string; url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : null });
      const path = url.split("?")[0];
      const reply =
        path === "/api/v1/map"
          ? { now: null, pending_events: 0, cards: [card], clusters: [{ key: "/r/app", label: "app", path: "~/r/app", sessions: 1, shown: 1, running: 0, waiting: 1, last_activity_at: null }] }
          : path === "/api/v1/attention"
            ? { items: [approval] }
            : path.endsWith("/responses")
              ? { action: { action_id: "a", session_id: "s1", type: "approval", state: "submitted", error: null } }
              : path === "/api/v1/sessions/s1/activity"
                ? { session_id: "s1", cluster: "/r/app", lanes: [] }
                : {};
      return new Response(JSON.stringify(reply), { status: 200 });
    }),
  );
  render(
    <MapView
      ws={{ ready: true, feed: [], sessions: {}, attentionVersion: 0, streamState: "live" }}
      tabs={null}
      onOpenSession={() => {}}
      onOpenAttention={() => {}}
    />,
  );
  const chip = await screen.findByText("Needs permission");
  const el = chip.closest("[data-card]") as HTMLElement;
  expect(el.className).toContain("permission");
  expect(el.textContent).toContain("Review request ↓");
  fireEvent.click(el);
  expect(await screen.findByText("git push")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/attention/att-1/responses"))).toBe(true));
  const sent = calls.find((c) => c.url.endsWith("/attention/att-1/responses"))!;
  expect(sent.body).toMatchObject({ decision: "allow" });
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("a session asking a question turns its card amber and its panel shows the question and options", async () => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  const question: AttentionItem = {
    attention_id: "att-2",
    kind: "question",
    session_id: "s1",
    task_id: null,
    state: "open",
    title: "How should a workflow appear?",
    detail: {
      tool: "AskUserQuestion",
      answer_in: "terminal",
      questions: [
        {
          question: "How should a workflow appear?",
          header: "Workflows",
          multi: false,
          options: [{ label: "Card + its agents", description: "Agents appear under it" }],
        },
      ],
    },
    allowed_responses: [],
    created_at: "2026-10-17T09:59:30Z",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      const reply =
        path === "/api/v1/map"
          ? { now: null, pending_events: 0, cards: [card], clusters: [{ key: "/r/app", label: "app", path: "~/r/app", sessions: 1, shown: 1, running: 0, waiting: 1, last_activity_at: null }] }
          : path === "/api/v1/attention"
            ? { items: [question] }
            : path === "/api/v1/sessions/s1/activity"
              ? { session_id: "s1", cluster: "/r/app", lanes: [] }
              : {};
      return new Response(JSON.stringify(reply), { status: 200 });
    }),
  );
  render(
    <MapView
      ws={{ ready: true, feed: [], sessions: {}, attentionVersion: 0, streamState: "live" }}
      tabs={null}
      onOpenSession={() => {}}
      onOpenAttention={() => {}}
    />,
  );
  const chip = await screen.findByText("Needs an answer");
  const el = chip.closest("[data-card]") as HTMLElement;
  expect(el.className).toContain("question");
  expect(el.textContent).toContain("See question ↓");
  fireEvent.click(el);
  expect(await screen.findByText("Card + its agents")).toBeTruthy();
  expect(screen.getByText("Answer it in the session's terminal.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("a question from a session Robin runs has a form, and says the CLI works too", () => {
  const item: AttentionItem = {
    attention_id: "att-9",
    kind: "question",
    session_id: "s1",
    task_id: null,
    state: "open",
    title: "Which database?",
    detail: {
      tool: "AskUserQuestion",
      answer_in: "robin",
      questions: [{ question: "Which database?", header: "", multi: false, options: [{ label: "SQLite", description: "" }] }],
    },
    allowed_responses: ["answer", "deny"],
    created_at: "2026-10-17T09:59:30Z",
  };
  const { container } = render(<QuestionList item={item} />);
  expect(container.textContent).toContain("Or answer it with robin chat s1 or robin answer att-9.");
  expect(container.querySelector("button[type=submit]")).not.toBeNull();
  expect(container.textContent).not.toContain("terminal");
});

function hookQuestion(over: Partial<AttentionItem> = {}): AttentionItem {
  return {
    attention_id: "att-h",
    kind: "question",
    session_id: "s1",
    task_id: null,
    state: "open",
    title: "Which database?",
    detail: {
      tool: "AskUserQuestion",
      answer_in: "hook",
      until: NOW / 1000 + 300,
      questions: [
        {
          question: "Which database?",
          header: "",
          multi: false,
          options: [
            { label: "SQLite", description: "" },
            { label: "Postgres", description: "" },
          ],
        },
      ],
    },
    allowed_responses: ["answer", "deny", "terminal"],
    created_at: "2026-10-17T09:59:30Z",
    ...over,
  };
}

function captureResponses() {
  const sent: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/attention/att-h/responses")) sent.push(JSON.parse(String(init?.body)));
      return new Response(
        JSON.stringify({ action: { action_id: "a", session_id: "s1", type: "approval", state: "submitted", error: null } }),
        { status: 202 },
      );
    }),
  );
  return sent;
}

test("a terminal session's question is answered from its form once every question has an answer", async () => {
  const sent = captureResponses();
  const onAnswered = vi.fn();
  render(<QuestionList item={hookQuestion()} onAnswered={onAnswered} />);
  const submit = screen.getByRole("button", { name: "Submit" }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("Postgres"));
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]).toMatchObject({ decision: "answer", answers: { "Which database?": "Postgres" } });
  await waitFor(() => expect(onAnswered).toHaveBeenCalled());
  vi.unstubAllGlobals();
});

test("a multi-select question sends every pick, with the user's own text as one of them", async () => {
  const sent = captureResponses();
  const item = hookQuestion();
  item.detail.questions = [
    {
      question: "Which features?",
      header: "",
      multi: true,
      options: [
        { label: "Auth", description: "" },
        { label: "Billing", description: "" },
      ],
    },
  ];
  render(<QuestionList item={item} />);
  fireEvent.click(screen.getByLabelText("Auth"));
  fireEvent.click(screen.getByLabelText("Billing"));
  fireEvent.click(screen.getByLabelText("Billing")); // unpicked again
  const own = screen.getByLabelText("Your own answer to Which features?");
  fireEvent.focus(own);
  fireEvent.change(own, { target: { value: "Search" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]).toMatchObject({ decision: "answer", answers: { "Which features?": ["Auth", "Search"] } });
  vi.unstubAllGlobals();
});

test("Decline and Answer in terminal send their decisions without answers", async () => {
  const sent = captureResponses();
  const { unmount } = render(<QuestionList item={hookQuestion()} />);
  fireEvent.click(screen.getByRole("button", { name: "Decline" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  unmount();
  render(<QuestionList item={hookQuestion()} />);
  fireEvent.click(screen.getByRole("button", { name: "Answer in terminal" }));
  await waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[0]).toMatchObject({ decision: "deny" });
  expect(sent[1]).toMatchObject({ decision: "terminal" });
  expect(sent[0]).not.toHaveProperty("answers");
  expect(sent[1]).not.toHaveProperty("answers");
  vi.unstubAllGlobals();
});

test("a question from a session Robin runs can't be handed to a terminal", () => {
  render(
    <QuestionList
      item={hookQuestion({
        allowed_responses: ["answer", "deny"],
        detail: { ...hookQuestion().detail, answer_in: "robin", until: undefined },
      })}
    />,
  );
  expect(screen.getByRole("button", { name: "Decline" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Answer in terminal" })).toBeNull();
});
