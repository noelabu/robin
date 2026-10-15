import type { Session } from "./api";

/** What the workspace is looking at: everything, a project, or a task. */
export type Scope = { kind: "all" } | { kind: "project"; id: string } | { kind: "task"; id: string };

export function inScope(s: Session, scope: Scope, taskSessions: Set<string> | null): boolean {
  if (scope.kind === "project") return s.project_id === scope.id;
  if (scope.kind === "task") return s.current_task_id === scope.id || (taskSessions?.has(s.session_id) ?? false);
  return true;
}
