import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { Runs } from "../components/Runs";

vi.mock("../api", async (orig) => {
  const m = await orig<typeof import("../api")>();
  const git = {
    repo_root: "/r", branch: "main", base_commit: "abcdef1234567", dirty_at_start: 2, dirty_at_end: 3,
    end_commit: "abcdef1234567", truncated: false, start_late: false, shared_folder: true,
    changed_files: [{ path: "<img src=x onerror=alert(1)>.ts", change: "modified" }],
  };
  return {
    ...m,
    api: {
      ...m.api,
      runs: async () => ({
        runs: [
          { run_id: "r2", started_at: "2026-10-16T01:00:00Z", ended_at: null, outcome: null, task_id: null,
            git: { ...git, changed_files: null, shared_folder: false } },
          { run_id: "r0", started_at: "2026-10-15T23:00:00Z", ended_at: "2026-10-15T23:00:01Z", outcome: "completed",
            task_id: null, git: { ...git, base_commit: null, dirty_at_start: null, start_late: true, changed_files: null, shared_folder: false } },
          { run_id: "r1", started_at: "2026-10-16T00:00:00Z", ended_at: "2026-10-16T00:01:00Z", outcome: "completed",
            task_id: null, git },
        ],
      }),
    },
  };
});

test("runs show base commit, start state, changed files and what can't be vouched for", async () => {
  const { container } = render(<Runs sessionId="s1" updatedAt="x" />);
  await waitFor(() => expect(screen.getAllByText(/2 uncommitted file\(s\) at start/)).toHaveLength(2));
  expect(screen.getByText("Changed files are listed when the run ends.")).toBeTruthy();
  expect(screen.getByText(/^main · start state unknown/)).toBeTruthy(); // never "no commits yet · clean"
  expect(screen.getByText(/Changed files unknown/)).toBeTruthy();
  expect(screen.getByText(/Another session worked in this folder/)).toBeTruthy();
  expect(container.querySelector("code")?.textContent).toBe("<img src=x onerror=alert(1)>.ts");
  expect(container.querySelector("img")).toBeNull();
});
