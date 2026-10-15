import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Robin approval gate for Pi. Pi has no built-in approvals, so Robin loads this
// extension (`pi -e`) and answers the confirm dialog over the RPC extension UI.
// It asks before the calls Codex would ask about, using the rule from Pi's
// extension docs: anything not read-only that may be destructive or reach the network.
export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
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
}
