// Robin's observer for Pi, installed by `robin hooks install pi` and removed by `robin hooks uninstall pi`.
// It reports what happens in a session to the local Robin service. It never blocks, changes or answers
// anything: every handler returns nothing, and the relay runs detached so Pi never waits for it.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";

const RELAY: string[] = __ROBIN_RELAY_ARGV__;

export default function (pi: ExtensionAPI) {
  const send = (name: string, ctx: any, extra: Record<string, unknown> = {}): void => {
    try {
      const sm = ctx?.sessionManager;
      const body = JSON.stringify({
        hook_event_name: name,
        session_id: sm?.getSessionId?.(),
        session_file: sm?.getSessionFile?.(),
        cwd: ctx?.cwd ?? sm?.getCwd?.(),
        ...extra,
      });
      const child = spawn(RELAY[0], RELAY.slice(1), { stdio: ["pipe", "ignore", "ignore"], detached: true });
      child.on("error", () => {});
      child.stdin?.on("error", () => {});
      child.stdin?.end(body);
      child.unref();
    } catch {
      // observing must never disturb the session
    }
  };

  pi.on("session_start", async (event, ctx) => {
    send("session_start", ctx, { reason: event.reason });
  });
  pi.on("turn_start", async (_event, ctx) => {
    send("turn_start", ctx);
  });
  pi.on("tool_call", async (event, ctx) => {
    send("tool_call", ctx, { tool_name: event.toolName, tool_call_id: event.toolCallId });
    return undefined;
  });
  pi.on("tool_result", async (event, ctx) => {
    send("tool_result", ctx, { tool_call_id: event.toolCallId });
    return undefined;
  });
  pi.on("agent_end", async (_event, ctx) => {
    send("agent_end", ctx);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    send("session_shutdown", ctx);
  });
}
