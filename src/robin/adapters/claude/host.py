"""The Claude Code runtime host, through the Claude Agent SDK (Phase 0: claude/findings.md).

Runs inside a per-session runtime host under the supervisor. Uses the owner's own `claude` login through
the unmodified bundled Claude Code binary; Robin never reads the token. Loads the user's Claude Code
settings and never chooses a permission mode more permissive than the harness default (FR-012).

Input sent during a turn is queued by Claude Code until the turn ends; Robin labels it "queued".
"""

import argparse
import asyncio
import json
import os
from typing import Any

from robin import __version__
from robin.adapters.claude.translate import translate
from robin.runtime.host import Approvals, emit, runtime, stdin_commands


def deny_read_settings(store: str) -> str:
    """Review independence (TR-064): the OS sandbox blocks shell reads, a deny rule blocks file tools."""
    return json.dumps(
        {
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"denyRead": [store]},
            },
            "permissions": {"deny": [f"Read(/{store}/**)"]},  # "//" + absolute path
        }
    )


@runtime("claude")
async def run_claude(args: argparse.Namespace) -> None:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
    )

    approvals = Approvals()
    state = {"busy": False, "interrupted": False}

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        decision = await approvals.request(tool_name, input_data)
        if decision.get("allow"):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=decision.get("message") or "Denied in Robin")

    deny = os.environ.get("ROBIN_DENY_READ")
    options = ClaudeAgentOptions(
        cwd=args.cwd,
        setting_sources=["user", "project", "local"],
        permission_mode="default",
        can_use_tool=can_use_tool,
        resume=os.environ.get("ROBIN_RESUME_NATIVE_ID") or None,
        settings=deny_read_settings(deny) if deny else None,
        env={"CLAUDE_AGENT_SDK_CLIENT_APP": f"robin/{__version__}"},
    )
    emit({"type": "host_ready", "runtime": "claude", "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN")})

    async with ClaudeSDKClient(options=options) as client:

        async def pump() -> None:
            async for message in client.receive_messages():
                is_result = type(message).__name__ == "ResultMessage"
                for event in translate(message, interrupted=is_result and state["interrupted"]):
                    emit(event)
                if is_result:
                    state["busy"] = state["interrupted"] = False
                    approvals.expire_all("turn ended")

        async def commands() -> None:
            async for c in stdin_commands():
                action_id = c.get("action_id")
                was_busy = state["busy"]
                try:
                    match c.get("cmd"):
                        case "send":
                            mode = "queued" if was_busy else "new_turn"
                            if not was_busy:
                                emit({"type": "turn_started"})
                            state["busy"] = True
                            await client.query(c.get("text", ""))
                            emit({"type": "input_submitted", "mode": mode, "action_id": action_id})
                        case "interrupt":
                            approvals.expire_all("interrupted")
                            state["interrupted"] = state["busy"]
                            await client.interrupt()
                            emit({"type": "interrupt_acknowledged", "action_id": action_id})
                        case "approval":
                            approvals.answer(c.get("request_id", ""), c)
                        case "stop":
                            return
                        case other:
                            emit(
                                {
                                    "type": "command_rejected",
                                    "reason": f"unknown command {other!r}",
                                    "action_id": action_id,
                                }
                            )
                except Exception as exc:  # report and keep the session alive
                    if c.get("cmd") == "send" and not was_busy:
                        state["busy"] = False
                        emit({"type": "turn_failed", "reason": "input was not accepted"})
                    emit(
                        {
                            "type": "command_failed",
                            "cmd": c.get("cmd"),
                            "reason": repr(exc),
                            "action_id": action_id,
                        }
                    )

        pump_task = asyncio.create_task(pump())
        command_task = asyncio.create_task(commands())
        done, _ = await asyncio.wait({pump_task, command_task}, return_when=asyncio.FIRST_COMPLETED)
        if pump_task in done and not pump_task.cancelled() and pump_task.exception() is not None:
            # The SDK stream broke: say so and end the host, so the supervisor sees the exit.
            if state["busy"]:
                emit({"type": "turn_failed", "reason": "runtime event stream failed"})
            emit({"type": "host_error", "reason": repr(pump_task.exception())})
        approvals.expire_all("session stopped")
        for task in (pump_task, command_task):
            task.cancel()
    emit({"type": "session_stopping"})
