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
  created_at: string;
  updated_at: string;
  controls: Record<"message" | "interrupt" | "stop" | "approval", Control>;
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

export type Workspace = { watermark: number; sessions: Session[]; supervisor: { connected: boolean } };

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
  launch: (runtime: string, cwd: string, prompt: string | null) =>
    request<{ action: Action; session_id: string }>("POST", "/sessions/launch", {
      runtime,
      cwd,
      prompt: prompt || null,
      action_id: newActionId(),
    }),
  act: (sessionId: string, body: { type: string; text?: string; request_id?: string; allow?: boolean }) =>
    request<{ action: Action }>("POST", `/sessions/${sessionId}/actions`, { ...body, action_id: newActionId() }),
  action: (actionId: string) => request<Action>("GET", `/actions/${actionId}`),
  attention: () => request<{ items: AttentionItem[] }>("GET", "/attention"),
  respond: (attentionId: string, decision: "allow" | "deny") =>
    request<{ action: Action }>("POST", `/attention/${attentionId}/responses`, { decision, action_id: newActionId() }),
  notice: (attentionId: string, to: "acknowledge" | "dismiss") =>
    request<AttentionItem>("POST", `/attention/${attentionId}/${to}`),
  adapters: () => request<{ adapters: { runtime: string; level: string }[] }>("GET", "/adapters"),
};
