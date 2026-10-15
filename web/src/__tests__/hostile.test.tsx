import { render, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { Project } from "../api";
import { TaskDetail } from "../components/TaskDetail";

// Agent-written findings and syntheses, and user-written tasks, render as text, never as markup (TR-070).
vi.mock("../api", async (orig) => {
  const H = '<img src=x onerror="alert(1)"><script>alert(2)</script><a href="javascript:alert(3)">x</a>';
  const m = await orig<typeof import("../api")>();
  const finding = {
    finding_id: "f1", version: 1, publication_path: H, summary: H, author: { session_id: "s", runtime: H, role: H },
    claims: [{ text: H, evidence: [{ path: H, range: H }] }], recommendations: [H], questions: [H], parents: [],
    task_id: "t1",
  };
  const synthesis = { artifact_id: "s1", kind: "synthesis", version: 1, content: { title: H, label: H, text: H } };
  return {
    ...m,
    api: {
      ...m.api,
      task: async () => ({
        task_id: "t1", project_id: "p1", title: H, objective: H, status: "open", completion_evidence: H,
        archived: false, revision: 1, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z",
        attachments: [{ attachment_id: "a", kind: "file", source_path: H, text: H, content_hash: "h", size: 1 }],
        sessions: [],
      }),
      findings: async () => ({ findings: [{ finding_id: "f1" }] }),
      finding: async () => finding,
      artifacts: async () => ({ artifacts: [synthesis] }),
      artifact: async () => synthesis,
    },
  };
});

test("task, finding and synthesis text never becomes markup", async () => {
  const project = { project_id: "p1", name: "<b>x</b>", path: "<i>p</i>" } as unknown as Project;
  const { container } = render(
    <TaskDetail
      taskId="t1"
      project={project}
      sessions={[]}
      refreshKey={0}
      onChanged={() => {}}
      onLaunch={() => {}}
      onOpenSession={() => {}}
    />,
  );
  await waitFor(() => expect(container.textContent).toContain("onerror"));
  expect(container.querySelector("img, script, b, i, a[href^='javascript']")).toBeNull();
});
