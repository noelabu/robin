"""The Codex runtime host, through `codex app-server` over stdio JSON-RPC (Phase 0: codex/findings.md).

Uses the owner's existing Codex login. Sends no approval policy or sandbox, so ~/.codex/config.toml applies;
only ever asks for something stricter (FR-012), or for no approvals when the user opted in for this session.
The sandbox is never loosened. Steering applies mid-turn (turn/steer).
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from robin.adapters.codex.translate import translate
from robin.runtime.host import Approvals, StderrTail, emit, runtime, stdin_commands
from robin.supervisor.protocol import MAX_LINE

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


def approval_policy(env: Mapping[str, str]) -> str | None:
    """A stricter override wins; otherwise no approvals if the user opted in; otherwise the user's default."""
    stricter = env.get("ROBIN_CODEX_APPROVAL_POLICY")
    if stricter in ("untrusted", "on-request"):  # only ever stricter than the user's default
        return stricter
    return "never" if env.get("ROBIN_SKIP_PERMISSIONS") == "1" else None


class Rpc:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.next_id = 0
        self.waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.closed: str | None = None

    async def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        if self.closed:
            raise ConnectionError(self.closed)
        self.next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[self.next_id] = fut
        try:
            await self.write({"id": self.next_id, "method": method, "params": params})
        except ConnectionError:  # the process is gone; close() fails the call with the reason
            pass
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise TimeoutError(f"codex app-server didn't answer {method} within {timeout:g}s") from None

    def close(self, reason: str) -> None:
        """The process exited: every call still waiting fails now, rather than at its timeout."""
        self.closed = reason
        for fut in self.waiting.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self.waiting.clear()

    async def write(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()


def deny_read_config(store: str) -> dict[str, Any]:
    """Review independence (TR-064): a permission profile denying the artifact store (Seatbelt-enforced)."""
    return {
        "default_permissions": "robin_review",
        "permissions": {"robin_review": {"extends": ":read-only", "filesystem": {store: "deny"}}},
    }


@runtime("codex")
async def run_codex(args: argparse.Namespace) -> None:
    approvals = Approvals()
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
        cwd=args.cwd,
    )
    stderr = StderrTail(proc.stderr)
    rpc = Rpc(proc)
    state: dict[str, Any] = {"thread": None, "turn": None}

    async def approve(msg: dict[str, Any]) -> None:
        method, params = msg["method"], msg.get("params") or {}
        if method in APPROVAL_METHODS:
            decision = await approvals.request(method, params)
            await rpc.write(
                {"id": msg["id"], "result": {"decision": "accept" if decision.get("allow") else "decline"}}
            )
        elif method == "mcpServer/elicitation/request":
            decision = await approvals.request(method, params)
            result = {"action": "accept", "content": {}} if decision.get("allow") else {"action": "decline"}
            await rpc.write({"id": msg["id"], "result": result})
        else:  # never leave a server request hanging
            emit({"type": "codex_unhandled_request", "method": method})
            await rpc.write({"id": msg["id"], "error": {"code": -32601, "message": "not supported by Robin"}})

    async def pump() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            msg = json.loads(line)
            if "id" in msg and "method" not in msg:
                fut = rpc.waiting.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif "id" in msg:
                asyncio.create_task(approve(msg))
            else:
                params = msg.get("params") or {}
                for event in translate(msg["method"], params):
                    if event["type"] == "turn_started":
                        state["turn"] = event.get("native_turn_id")
                    elif event["type"] in ("turn_completed", "turn_interrupted", "turn_failed"):
                        state["turn"] = None
                        approvals.expire_all("turn ended")
                    emit(event)
        reason = await stderr.exit_reason("codex app-server", await proc.wait())
        rpc.close(reason)
        emit({"type": "host_error", "reason": reason})

    pump_task = asyncio.create_task(pump())
    init = await rpc.call("initialize", {"clientInfo": {"name": "robin", "version": "0.1"}})
    if "error" in init:
        emit({"type": "host_error", "reason": f"initialize failed: {init['error']}"})
        raise SystemExit(2)
    await rpc.write({"method": "initialized"})
    start: dict[str, Any] = {"cwd": args.cwd}
    if policy := approval_policy(os.environ):
        start["approvalPolicy"] = policy
    if os.environ.get("ROBIN_DENY_READ"):
        start["config"] = deny_read_config(os.environ["ROBIN_DENY_READ"])
    if "tool" in os.environ.get("ROBIN_FINDINGS", ""):  # finding publication for task-bound sessions
        config = start.setdefault("config", {})
        config.setdefault("mcp_servers", {})["robin"] = {
            "command": sys.executable,
            "args": ["-m", "robin.findings.mcp_server"],
            # Codex starts MCP servers with a clean environment; pass the session's credential through by
            # name so the token never lands in Codex's own config or history.
            "env_vars": ["ROBIN_AGENT_TOKEN", "ROBIN_SERVICE_URL"],
        }
    resume = os.environ.get("ROBIN_RESUME_NATIVE_ID")
    reply = await (
        rpc.call("thread/resume", {**start, "threadId": resume})
        if resume
        else rpc.call("thread/start", start)
    )
    if "error" in reply:
        emit({"type": "host_error", "reason": f"thread start failed: {reply['error']}"})
        raise SystemExit(2)
    result = reply["result"]
    state["thread"] = result["thread"]["id"]
    emit(
        {
            "type": "session_started",
            "native_session_id": state["thread"],
            "model": result.get("model"),
            "approval_policy": result.get("approvalPolicy"),
            "sandbox": result.get("sandbox"),
            "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN"),
        }
    )

    async def commands() -> None:
        async for c in stdin_commands():
            action_id = c.get("action_id")
            try:
                match c.get("cmd"):
                    case "send":
                        text = [{"type": "text", "text": c.get("text", "")}]
                        if state["turn"]:
                            r = await rpc.call(
                                "turn/steer",
                                {"threadId": state["thread"], "expectedTurnId": state["turn"], "input": text},
                            )
                            mode = "steer"
                        else:
                            r = await rpc.call("turn/start", {"threadId": state["thread"], "input": text})
                            mode = "new_turn"
                        if "error" in r:
                            emit(
                                {
                                    "type": "command_failed",
                                    "cmd": "send",
                                    "reason": str(r["error"]),
                                    "action_id": action_id,
                                }
                            )
                        else:
                            emit({"type": "input_submitted", "mode": mode, "action_id": action_id})
                    case "interrupt":
                        approvals.expire_all("interrupted")
                        if not state["turn"]:
                            emit(
                                {
                                    "type": "command_rejected",
                                    "reason": "no turn is running",
                                    "action_id": action_id,
                                }
                            )
                            continue
                        r = await rpc.call(
                            "turn/interrupt", {"threadId": state["thread"], "turnId": state["turn"]}
                        )
                        if "error" in r:
                            emit(
                                {
                                    "type": "command_failed",
                                    "cmd": "interrupt",
                                    "reason": str(r["error"]),
                                    "action_id": action_id,
                                }
                            )
                        else:
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
            except Exception as exc:
                emit(
                    {
                        "type": "command_failed",
                        "cmd": c.get("cmd"),
                        "reason": repr(exc),
                        "action_id": action_id,
                    }
                )

    command_task = asyncio.create_task(commands())
    await asyncio.wait({pump_task, command_task}, return_when=asyncio.FIRST_COMPLETED)
    approvals.expire_all("session stopped")
    command_task.cancel()
    if proc.stdin:
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        proc.terminate()
    pump_task.cancel()
    emit({"type": "session_stopping"})
