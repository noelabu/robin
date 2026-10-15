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
// Finding publication: when ROBIN_PUBLISH_PATH is "tool", registers
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

  if (process.env.ROBIN_PUBLISH_PATH !== "tool") return;
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
}
