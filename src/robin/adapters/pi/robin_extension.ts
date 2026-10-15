import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// Robin's Pi extension, loaded with `pi -e`.
//
// Approval gate: Pi has no built-in approvals, so Robin answers this confirm
// dialog over the RPC extension UI. It asks before the calls Codex would ask
// about, using the rule from Pi's extension docs: anything not read-only that
// may be destructive or reach the network. Pi's built-in tools carry no
// annotations, so the read-only built-ins are listed explicitly.
//
// Finding publication: when ROBIN_FINDINGS includes "tool" (task-bound sessions), registers
// robin_finding_publish. The tool only validates and replies; Robin takes the
// finding from the tool_execution_start event in the session's own stream.

const READ_ONLY_BUILTINS = new Set(["read", "grep", "find", "ls"]);

const Evidence = Type.Object({ path: Type.String(), range: Type.Optional(Type.String()) });
const Claim = Type.Object({
  text: Type.String(),
  evidence: Type.Optional(Type.Array(Evidence)),
  unsupported: Type.Optional(Type.Boolean()),
});
const FindingParams = Type.Object({
  submission_id: Type.Optional(Type.String({ description: "Your own ID; reuse it only to retry the same finding" })),
  summary: Type.String(),
  claims: Type.Array(Claim),
  recommendations: Type.Optional(Type.Array(Type.String())),
  questions: Type.Optional(Type.Array(Type.String())),
});

export default function (pi: ExtensionAPI) {
  // On by default for Robin-launched Pi sessions (owner decision 2026-10-15); ROBIN_PI_APPROVALS=0 turns it off.
  if (process.env.ROBIN_PI_APPROVALS !== "0")
  pi.on("tool_call", async (event, ctx) => {
    if (READ_ONLY_BUILTINS.has(event.toolName)) return;
    const hints = pi.getAllTools().find((tool) => tool.name === event.toolName)?.annotations;
    const needsApproval =
      hints?.destructiveHint === true ||
      (!hints?.readOnlyHint && ((hints?.destructiveHint ?? true) || (hints?.openWorldHint ?? true)));
    if (!needsApproval) return;
    const detail = JSON.stringify({ tool: event.toolName, input: event.input });
    if (!(await ctx.ui.confirm("robin:approval", detail))) {
      return { block: true, reason: `${event.toolName} was not approved in Robin` };
    }
  });

  if (!(process.env.ROBIN_FINDINGS ?? process.env.ROBIN_PUBLISH_PATH ?? "").includes("tool")) return;
  pi.registerTool({
    name: "robin_finding_publish",
    label: "Publish finding",
    description:
      "Publish one structured finding to the Robin task. Robin records who published it from the session " +
      'itself. Every claim needs evidence (a file path and line range) unless it sets "unsupported": true.',
    parameters: FindingParams,
    annotations: { readOnlyHint: true, openWorldHint: false },
    async execute(_toolCallId, params) {
      const errors: string[] = [];
      if (!params.summary?.trim()) errors.push("summary must be a non-empty string");
      if (!params.claims?.length) errors.push("claims must be a non-empty list");
      params.claims?.forEach((c, i) => {
        if (!c.unsupported && !c.evidence?.length) errors.push(`claims[${i}] needs evidence or "unsupported": true`);
      });
      const text = errors.length
        ? `REJECTED: ${errors.join("; ")}. Fix these and call robin_finding_publish again.`
        : "ACCEPTED: Robin recorded this finding.";
      return { content: [{ type: "text", text }] };
    },
  });

  // Retrieval tools (TR-066): scoped by this session's own Robin credential to what the user granted it.
  const url = process.env.ROBIN_SERVICE_URL;
  const token = process.env.ROBIN_AGENT_TOKEN;
  if (!url || !token) return;
  const callRobin = async (method: string, path: string, body?: unknown): Promise<string> => {
    try {
      const res = await fetch(`${url}/api/v1${path}`, {
        method,
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await res.text();
      if (res.ok) return text;
      try {
        return `Robin refused: ${JSON.parse(text).error.message}`;
      } catch {
        return `Robin refused: HTTP ${res.status}`;
      }
    } catch {
      return "Robin isn't reachable right now; try again later.";
    }
  };
  const readOnly = { readOnlyHint: true, openWorldHint: false };
  const tools: [string, string, any, (p: any) => Promise<string>][] = [
    ["robin_task_get", "Get this session's Robin task: title, objective, instructions and expected outputs.",
      Type.Object({}), () => callRobin("GET", "/agent/task")],
    ["robin_finding_list", "List the findings you can read: your own, and those the user shared with you.",
      Type.Object({}), () => callRobin("GET", "/agent/findings")],
    ["robin_finding_read", "Read one finding you can see (robin_finding_list shows which).",
      Type.Object({ finding_id: Type.String() }),
      (p) => callRobin("GET", `/agent/findings/${encodeURIComponent(p.finding_id)}`)],
    ["robin_session_search", "Search the transcript ranges the user shared with you. Returns cursors and snippets.",
      Type.Object({ query: Type.String() }), (p) => callRobin("POST", "/agent/search", p)],
    ["robin_session_read_excerpt", "Read messages from a transcript range the user shared with you.",
      Type.Object({ session_id: Type.String(), after: Type.Integer(), through: Type.Integer() }),
      (p) => callRobin("POST", "/agent/excerpt", p)],
    ["robin_artifact_publish",
      "Save a written result (for example a synthesis) to the Robin task, listing the finding IDs it is based on " +
        "as inputs. New claims need their own evidence.",
      Type.Object({ title: Type.String(), text: Type.String(), inputs: Type.Optional(Type.Array(Type.String())) }),
      (p) => callRobin("POST", "/agent/artifacts", { ...p, inputs: p.inputs ?? [] })],
  ];
  for (const [name, description, parameters, run] of tools) {
    pi.registerTool({
      name,
      label: name.replace("robin_", "Robin: ").replaceAll("_", " "),
      description,
      parameters,
      annotations: readOnly,
      async execute(_toolCallId: string, params: any) {
        return { content: [{ type: "text", text: await run(params) }] };
      },
    });
  }
}
