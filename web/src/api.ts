// The Robin service API (/api/v1). Same origin; the session cookie is HttpOnly, the CSRF token lives in memory.

export type Control = { enabled: boolean; reason: string | null };

export type Session = {
  session_id: string;
  runtime: string;
  native_session_id: string | null;
  mode: "managed" | "observed";
  runtime_state: "running" | "waiting_for_input" | "idle" | "failed" | "stopped" | "unknown";
  connection_state: "connected" | "reconnecting" | "stale" | "disconnected";
  state_evidence_at: string | null;
  active_run_id: string | null;
  execution_cwd: string | null;
  lease_generation: number;
  control_owner: "robin" | "terminal" | null;
  project_id: string | null;
  current_task_id: string | null;
  archived: boolean;
  branch: string | null;
  shared_folder_with: string[];
  independence: "enforced" | "not_isolated" | null;
  created_at: string;
  updated_at: string;
  controls: Record<"message" | "interrupt" | "stop" | "approval" | "handoff" | "reclaim", Control>;
  capabilities: { runtime: string; steer_mid_turn: boolean; tested_versions: string; level: string };
};

export type RobinEvent = {
  global_cursor: number;
  event_id: string;
  session_id: string;
  runtime: string;
  event_family: string;
  event_type: string;
  evidence_kind: "reported" | "observed" | "inferred" | "unknown";
  source_time: string | null;
  ingested_at: string;
  payload_ref: string | null;
  task_id: string | null;
};

export type Project = {
  project_id: string;
  name: string;
  path: string;
  git: { remote: string | null; branch: string | null } | null;
  last_activity_at: string | null;
};

export type Task = {
  task_id: string;
  project_id: string;
  title: string;
  objective: string;
  status: "open" | "completed" | "abandoned";
  completion_evidence: string | null;
  archived: boolean;
  revision: number;
  updated_at: string;
};

export type TaskDetail = Task & {
  sessions: { session_id: string; runtime: string; role: string | null; runtime_state: string; current: boolean }[];
};

export type Action = {
  action_id: string;
  session_id: string;
  type: string;
  state: "requested" | "submitted" | "acknowledged" | "failed" | "cancelled" | "outcome_unknown";
  error: { code: string; message: string } | null;
};

export type AttentionItem = {
  attention_id: string;
  kind: "question" | "approval" | "failure" | "finding_review";
  session_id: string;
  task_id: string | null;
  state: "open" | "acknowledged" | "resolved" | "dismissed" | "expired" | "handed_off";
  title: string;
  detail: { tool?: string; input?: unknown; reason?: unknown; event?: string };
  allowed_responses: string[];
  created_at: string;
};

export type Evidence = { path?: string; range?: string; artifact_id?: string };
export type Claim = { text: string; evidence: Evidence[]; unsupported?: boolean };
export type Author = { kind: "user" | "agent"; runtime?: string; native_session_id?: string | null };

export type Finding = {
  finding_id: string;
  version: number;
  latest_version: number;
  task_id: string;
  author: Author;
  summary: string;
  claims: Claim[];
  recommendations: string[];
  questions: string[];
  publication_path: string;
  created_at: string;
  content_hash: string;
};

export type Packet = {
  packet_id: string;
  state: "previewed" | "confirmed" | "invalidated" | "delivered";
  invalid_reason: string | null;
  recipient: { session_id: string; runtime: string; independence: string | null };
  question: string;
  sources: { n: number; kind: string; id: string; label: string; content_hash: string; flags?: string[] }[];
  redactions: { source: number | string; rule_id: string }[];
  flags: string[];
  bytes: number;
  approx_tokens: number;
  payload_hash: string;
  deliveries: { action_id: string; state: Action["state"]; error: string | null }[];
  text?: string;
};

export type Artifact = {
  artifact_id: string;
  kind: string;
  version: number;
  inputs: { parent_artifact_id: string; parent_version: number; relation: string }[];
  content: { title?: string; text?: string; author?: Author; label?: string } & Record<string, unknown>;
};

export type Workspace = {
  watermark: number;
  sessions: Session[];
  projects: Project[];
  tasks: Task[];
  supervisor: { connected: boolean };
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public retry: string) {
    super(message);
  }
}

let csrf: string | null = null;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (csrf && method !== "GET") headers["x-robin-csrf"] = csrf;
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.retry ?? "do_not_retry");
  }
  return data as T;
}

/** Sign in from a #bootstrap=… link, or recover the CSRF token for an existing cookie session. */
export async function signIn(): Promise<boolean> {
  const match = window.location.hash.match(/bootstrap=([\w-]+)/);
  if (match) {
    history.replaceState(null, "", window.location.pathname); // the secret leaves the address bar at once
    try {
      csrf = (await request<{ csrf_token: string }>("POST", "/auth/exchange", { secret: match[1] })).csrf_token;
      return true;
    } catch {
      /* fall through: maybe an existing session */
    }
  }
  try {
    csrf = (await request<{ csrf_token: string }>("GET", "/auth/session")).csrf_token;
    return true;
  } catch {
    return false;
  }
}

export const newActionId = () => `ui-${crypto.randomUUID()}`;

export const api = {
  workspace: () => request<Workspace>("GET", "/workspace"),
  launch: (
    runtime: string,
    cwd: string | null,
    prompt: string | null,
    taskId: string | null = null,
    independent = false,
  ) =>
    request<{ action: Action; session_id: string; warnings: string[] }>("POST", "/sessions/launch", {
      runtime,
      cwd: cwd || null,
      prompt: prompt || null,
      task_id: taskId,
      independent,
      action_id: newActionId(),
    }),
  history: (scope: { session_id?: string; task_id?: string; project_id?: string }, limit = 200) =>
    request<{ events: (RobinEvent & { preview: string | null })[] }>(
      "GET",
      `/events?${new URLSearchParams({ ...scope, latest: "true", previews: "true", limit: String(limit) })}`,
    ),
  createProject: (path: string) => request<Project>("POST", "/projects", { path }),
  createTask: (projectId: string, title: string, objective: string) =>
    request<Task>("POST", "/tasks", { project_id: projectId, title, objective }),
  task: (taskId: string) => request<TaskDetail>("GET", `/tasks/${taskId}`),
  patchTask: (task: Task, changes: Partial<Pick<Task, "status" | "archived" | "completion_evidence">>) =>
    request<Task>("PATCH", `/tasks/${task.task_id}`, { expected_revision: task.revision, ...changes }),
  archiveSession: (sessionId: string, archived: boolean) =>
    request<{ archived: boolean }>("PATCH", `/sessions/${sessionId}`, { archived }),
  act: (
    sessionId: string,
    body: { type: string; text?: string; request_id?: string; allow?: boolean; interrupt_running?: boolean },
  ) =>
    request<{ action: Action }>("POST", `/sessions/${sessionId}/actions`, { ...body, action_id: newActionId() }),
  action: (actionId: string) => request<Action>("GET", `/actions/${actionId}`),
  attention: () => request<{ items: AttentionItem[] }>("GET", "/attention"),
  respond: (attentionId: string, decision: "allow" | "deny") =>
    request<{ action: Action }>("POST", `/attention/${attentionId}/responses`, { decision, action_id: newActionId() }),
  notice: (attentionId: string, to: "acknowledge" | "dismiss") =>
    request<AttentionItem>("POST", `/attention/${attentionId}/${to}`),
  terminal: (sessionId: string) =>
    request<{ mode: string; command: string[]; cwd: string | null; owner: string | null }>(
      "GET",
      `/sessions/${sessionId}/terminal`,
    ),
  openTerminal: (sessionId: string) => request<{ opened: boolean }>("POST", `/sessions/${sessionId}/terminal/open`),
  adapters: () => request<{ adapters: { runtime: string; level: string }[] }>("GET", "/adapters"),
  findings: (taskId: string) =>
    request<{ findings: { finding_id: string }[] }>("GET", `/findings?task_id=${encodeURIComponent(taskId)}`),
  finding: (id: string) => request<Finding>("GET", `/findings/${id}`),
  artifacts: (taskId: string, kind: string) =>
    request<{ artifacts: { artifact_id: string }[] }>(
      "GET",
      `/artifacts?task_id=${encodeURIComponent(taskId)}&kind=${kind}`,
    ),
  artifact: (id: string) => request<Artifact>("GET", `/artifacts/${id}`),
  projectFile: (projectId: string, path: string, range?: string) =>
    request<{ path: string; first_line: number; lines: string[]; truncated: boolean }>(
      "GET",
      `/projects/${projectId}/file?${new URLSearchParams(range ? { path, range } : { path })}`,
    ),
  previewContext: (body: {
    task_id: string;
    target_session_id: string;
    question: string;
    findings: { id: string; version: number; claims: number[] | null }[];
    include_objective: boolean;
  }) => request<Packet>("POST", "/context/preview", body),
  getContext: (id: string) => request<Packet>("GET", `/context/${id}`),
  confirmContext: (id: string, hash: string) =>
    request<Packet>("POST", `/context/${id}/confirm`, { payload_hash: hash }),
  deliverContext: (id: string) => request<Packet>("POST", `/context/${id}/deliver`, { action_id: newActionId() }),
  saveReply: (id: string) => request<{ artifact_id: string }>("POST", `/context/${id}/save-reply`, {}),
};
