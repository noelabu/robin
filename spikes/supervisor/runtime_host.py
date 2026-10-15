"""Phase 0 spike: a per-session runtime host (plan WP-1.3).

Runs one runtime session. Commands arrive as JSON lines on stdin; events
leave as JSON lines on stdout. The supervisor stays runtime-agnostic.

Runtimes:
  fake    scripted events, no network; used to test the supervisor
  claude  Claude Agent SDK (needs ANTHROPIC_API_KEY)

Commands: {"cmd": "send", "text": ...}, {"cmd": "interrupt"},
          {"cmd": "approval", "request_id": ..., "allow": bool, "message": ...},
          {"cmd": "stop"}
"""

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import uuid

from protocol import MAX_LINE


def emit(event: dict):
    sys.stdout.write(json.dumps(event, default=repr) + "\n")
    sys.stdout.flush()


async def stdin_commands():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        yield json.loads(line)


class Approvals:
    """Pending approval requests. They expire when the run is interrupted or ends (TR-045)."""

    def __init__(self):
        self.pending: dict[str, asyncio.Future] = {}

    async def request(self, tool: str, tool_input: dict) -> dict:
        rid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        emit({"type": "approval_request", "request_id": rid, "tool": tool, "input": tool_input})
        try:
            return await fut
        finally:
            self.pending.pop(rid, None)

    def answer(self, rid: str, decision: dict):
        fut = self.pending.get(rid)
        if fut is None or fut.done():
            emit({"type": "approval_rejected", "request_id": rid, "reason": "unknown_or_expired"})
            return
        fut.set_result(decision)
        emit({"type": "approval_answered", "request_id": rid, "allow": decision.get("allow")})

    def expire_all(self, reason: str):
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_result({"allow": False, "message": f"Request expired: {reason}", "expired": True})
                emit({"type": "approval_expired", "request_id": rid, "reason": reason})


# -- fake runtime -----------------------------------------------------------

async def run_fake(args):
    approvals = Approvals()
    native_id = uuid.uuid4().hex
    emit({"type": "session_started", "native_session_id": native_id,
          "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN")})
    turn = None

    async def work(text: str):
        emit({"type": "turn_started", "input": text})
        try:
            for n in range(1, 1_000_000):
                await asyncio.sleep(0.2)
                emit({"type": "tick", "n": n})
                if n == 5:
                    decision = await approvals.request("Bash", {"command": "make test"})
                    emit({"type": "tool_decision", "allow": decision["allow"]})
        except asyncio.CancelledError:
            emit({"type": "turn_interrupted"})
            raise

    async for c in stdin_commands():
        if c["cmd"] == "send":
            if turn and not turn.done():
                emit({"type": "input_queued", "text": c["text"]})
            else:
                turn = asyncio.create_task(work(c["text"]))
        elif c["cmd"] == "interrupt":
            approvals.expire_all("interrupted")
            if turn:
                turn.cancel()
            emit({"type": "interrupt_acknowledged"})
        elif c["cmd"] == "approval":
            approvals.answer(c["request_id"], c)
        elif c["cmd"] == "stop":
            break
    approvals.expire_all("session stopped")
    if turn:
        turn.cancel()
    emit({"type": "session_stopping"})


# -- Claude Agent SDK runtime -------------------------------------------------

def describe(message) -> dict:
    data = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else repr(message)
    return {"type": "sdk_message", "kind": type(message).__name__, "data": data}


async def run_claude(args):
    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,
                                  PermissionResultAllow, PermissionResultDeny)

    approvals = Approvals()

    async def can_use_tool(tool_name, input_data, context):
        decision = await approvals.request(tool_name, input_data)
        if decision.get("allow"):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=decision.get("message") or "Denied in Robin")

    options = ClaudeAgentOptions(
        cwd=args.cwd,
        setting_sources=["user", "project", "local"],  # load the user's harness settings (FR-012)
        permission_mode="default",                       # never more permissive than the harness default
        can_use_tool=can_use_tool,
        env={"CLAUDE_AGENT_SDK_CLIENT_APP": "robin-spike/0"},
    )
    emit({"type": "host_ready", "runtime": "claude",
          "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN")})
    async with ClaudeSDKClient(options=options) as client:
        async def pump():
            async for message in client.receive_messages():
                emit(describe(message))

        pump_task = asyncio.create_task(pump())
        async for c in stdin_commands():
            try:
                if c["cmd"] == "send":
                    await client.query(c["text"])
                    emit({"type": "input_submitted"})
                elif c["cmd"] == "interrupt":
                    approvals.expire_all("interrupted")
                    await client.interrupt()
                    emit({"type": "interrupt_submitted"})
                elif c["cmd"] == "approval":
                    approvals.answer(c["request_id"], c)
                elif c["cmd"] == "stop":
                    break
            except Exception as exc:  # report and keep the session alive
                emit({"type": "command_failed", "cmd": c.get("cmd"), "error": repr(exc)})
        approvals.expire_all("session stopped")
        pump_task.cancel()
    emit({"type": "session_stopping"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", choices=["fake", "claude"], required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()
    asyncio.run(run_fake(args) if args.runtime == "fake" else run_claude(args))
