import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { FileDiff } from "../api";
import { Changes } from "../components/Changes";
import { DiffView } from "../components/Diff";
import { Runs } from "../components/Runs";

const H = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
const hunk = {
  old_start: 40, old_lines: 3, new_start: 40, new_lines: 4, header: "def f():",
  lines: [" keep", "-old", `+${H}`, "+two", " tail", "\\ No newline at end of file"],
};
const ok: FileDiff = { path: "a.py", change: "modified", status: "ok", added: 2, removed: 1, truncated: false, hunks: [hunk] };
const runDiff = vi.fn(async (): Promise<FileDiff> => ok);
const changesDiff = vi.fn(async (): Promise<FileDiff> => ok);
const changes = vi.fn();

vi.mock("../api", async (orig) => {
  const m = await orig<typeof import("../api")>();
  const git = {
    repo_root: "/r", branch: "main", base_commit: "abc", dirty_at_start: 0, dirty_at_end: 0, end_commit: "abc",
    truncated: false, start_late: false, shared_folder: false,
  };
  return {
    ...m,
    api: {
      ...m.api,
      runs: async () => ({
        runs: [
          { run_id: "r1", started_at: "2026-10-26T00:00:00Z", ended_at: "2026-10-26T00:01:00Z", outcome: "completed",
            task_id: null, git: { ...git, changed_files: [{ path: "a.py", change: "modified", before: "b1", after: "b2" }] } },
        ],
      }),
      runDiff: (...a: unknown[]) => runDiff(...(a as [])),
      changes: (...a: unknown[]) => changes(...a),
      changesDiff: (...a: unknown[]) => changesDiff(...(a as [])),
    },
  };
});

beforeEach(() => {
  localStorage.clear();
  runDiff.mockClear();
  changesDiff.mockClear();
});

test("a diff numbers each side's lines and renders agent text as text", () => {
  const { container } = render(<DiffView diff={ok} layout="unified" />);
  const rows = [...container.querySelectorAll("tbody tr")].map((r) => [...r.querySelectorAll("td")].map((td) => td.textContent));
  expect(rows[0][0]).toBe("@@ -40,3 +40,4 @@ def f():");
  expect(rows.slice(1, 6)).toEqual([
    ["40", "40", " keep"],
    ["41", "", "-old"],
    ["", "41", `+${H}`],
    ["", "42", "+two"],
    ["42", "43", " tail"],
  ]);
  expect(container.querySelector("img, script")).toBeNull();
});

test("split pairs removed and added lines side by side", () => {
  const { container } = render(<DiffView diff={ok} layout="split" />);
  const rows = [...container.querySelectorAll("tbody tr")].slice(1).map((r) => [...r.querySelectorAll("td")].map((td) => td.textContent));
  expect(rows[1]).toEqual(["41", "old", "41", H]);
  expect(rows[2]).toEqual(["", "", "42", "two"]);
});

test.each([
  ["binary", "Binary file."],
  ["too_large", "Too large to show."],
  ["not_recorded", "Diff not available for this run."],
  ["unavailable", "No longer available."],
  ["unknown", "Couldn't read Git."],
] as const)("a %s diff says so", (status, text) => {
  render(<DiffView diff={{ ...ok, status, hunks: [] }} layout="unified" />);
  expect(screen.getByText(text)).toBeTruthy();
});

test("a run's changed file opens its diff, and the layout choice is remembered", async () => {
  const { unmount } = render(<Runs sessionId="s1" updatedAt="x" />);
  fireEvent.click(await screen.findByRole("button", { name: "a.py" }));
  await screen.findByText("+2 −1");
  expect(runDiff).toHaveBeenCalledWith("s1", "r1", "a.py");
  fireEvent.click(screen.getByRole("button", { name: "Split" }));
  expect(localStorage.getItem("robin.diff")).toBe("split");
  unmount();
  render(<Runs sessionId="s1" updatedAt="x" />);
  fireEvent.click(await screen.findByRole("button", { name: "a.py" }));
  await waitFor(() => expect(document.querySelector("table.diff.split")).toBeTruthy());
});

test("a browser that can't store the layout still switches it", async () => {
  const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  render(<Runs sessionId="s1" updatedAt="x" />);
  fireEvent.click(await screen.findByRole("button", { name: "a.py" }));
  fireEvent.click(await screen.findByRole("button", { name: "Split" }));
  expect(document.querySelector("table.diff.split")).toBeTruthy();
  set.mockRestore();
});

test("changes load only when opened and say why there are none", async () => {
  changes.mockResolvedValue({ state: "not_repo" });
  const { container } = render(<Changes sessionId="s1" updatedAt="x" />);
  expect(changes).not.toHaveBeenCalled();
  const details = container.querySelector("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await screen.findByText("Not a Git repository.");
});

test("a phone opens a changed file full screen, unified, and Escape closes only that sheet", async () => {
  changes.mockResolvedValue({
    state: "ok", repo_root: "/r", branch: null, head: "abcdef1234567", truncated: false,
    files: [{ path: "a.py", change: "modified", added: 2, removed: 1 }, { path: "x.png", change: "untracked", added: null, removed: null, binary: true }],
  });
  const { container } = render(<Changes sessionId="s1" updatedAt="x" compact />);
  const details = container.querySelector("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  expect(await screen.findByText("detached · abcdef1234")).toBeTruthy();
  expect(screen.getByText("binary")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "a.py" }));
  const sheet = await screen.findByRole("dialog", { name: "a.py" });
  expect(sheet.classList.contains("full")).toBe(true);
  await waitFor(() => expect(sheet.querySelector("table.diff.unified")).toBeTruthy());
  expect(screen.queryByRole("button", { name: "Split" })).toBeNull();
  expect(changesDiff).toHaveBeenCalledWith("s1", "a.py");
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

test("over another sheet, Escape closes only the top one", async () => {
  const { Sheet } = await import("../phone/Sheet");
  const outer = vi.fn();
  const inner = vi.fn();
  render(
    <Sheet label="Session" onClose={outer}>
      <Sheet full label="a.py" onClose={inner}>
        <p>diff</p>
      </Sheet>
    </Sheet>,
  );
  fireEvent.keyDown(document, { key: "Escape" });
  expect(inner).toHaveBeenCalledTimes(1);
  expect(outer).not.toHaveBeenCalled();
});
