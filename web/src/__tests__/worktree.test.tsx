import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { WorktreePanel } from "../components/WorktreePanel";

const removed: boolean[] = [];

vi.mock("../api", async (orig) => {
  const m = await orig<typeof import("../api")>();
  const wt = {
    worktree_id: "w1", session_id: "s1", path: "/wt/app/x", branch: "robin/x", from_branch: "main",
    base_commit: "abcdef1234567", removed_at: null,
  };
  return {
    ...m,
    api: {
      ...m.api,
      worktrees: async () => ({ worktrees: [wt] }),
      worktree: async () => ({ ...wt, exists: true, uncommitted: ["scratch.txt"], unmerged_commits: 2 }),
      removeWorktree: async (_: string, discard: boolean) => {
        removed.push(discard);
        return { ...wt, removed_at: "2026-10-16T00:00:00Z", exists: false, uncommitted: [], unmerged_commits: 2,
          branch_kept: "robin/x" };
      },
    },
  };
});

test("removing a worktree says what it loses and keeps the branch", async () => {
  const { rerender } = render(<WorktreePanel sessionId="s1" connected updatedAt="a" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Remove worktree…" })).toBeTruthy());
  expect((screen.getByRole("button", { name: "Remove worktree…" }) as HTMLButtonElement).disabled).toBe(true);

  rerender(<WorktreePanel sessionId="s1" connected={false} updatedAt="a" />);
  fireEvent.click(screen.getByRole("button", { name: "Remove worktree…" }));
  await waitFor(() => expect(screen.getByText(/1 uncommitted file\(s\) will be lost: scratch.txt/)).toBeTruthy());
  expect(screen.getByText(/2 commit\(s\) aren't on main; the branch robin\/x keeps them/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Discard changes and remove" }));
  await waitFor(() => expect(screen.getByText("Removed. Branch robin/x is kept.")).toBeTruthy());
  expect(removed).toEqual([true]);
});
