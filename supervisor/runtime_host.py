"""Phase 0 spike: a per-session runtime host (plan WP-1.3).

Runs one runtime session. Commands arrive as JSON lines on stdin; events
leave as JSON lines on stdout. The supervisor stays runtime-agnostic.

Runtimes:
  fake    scripted events, no network; used to test the supervisor
  claude  Claude Agent SDK (needs ANTHROPIC_API_KEY)
  codex   `codex app-server` over stdio JSON-RPC (uses the existing Codex login)
  pi      `pi --mode rpc` with Robin's approval extension (uses Pi's configured provider)

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

from findings import TOOL_DESCRIPTION, TOOL_SCHEMA, parse_message_blocks, submission_event, validate
from protocol import MAX_LINE

# Finding publication path for this session: "tool" (MCP / extension tool) or "message" (final-message block)
PUBLISH_PATH = os.environ.get("ROBIN_PUBLISH_PATH", "tool")
# Native session to resume through its original runtime (FR-013); empty starts a new session.
RESUME_ID = os.environ.get("ROBIN_RESUME_NATIVE_ID")


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
    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                                  PermissionResultDeny, ResultMessage, create_sdk_mcp_server, tool)

    approvals = Approvals()

    async def can_use_tool(tool_name, input_data, context):
        decision = await approvals.request(tool_name, input_data)
        if decision.get("allow"):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=decision.get("message") or "Denied in Robin")

    @tool("finding_publish", TOOL_DESCRIPTION, TOOL_SCHEMA)
    async def finding_publish(payload):
        emit(submission_event("tool", payload, native_ref=uuid.uuid4().hex))
        errors = validate(payload)
        text = ("REJECTED: " + "; ".join(errors)) if errors else "ACCEPTED: Robin recorded this finding."
        return {"content": [{"type": "text", "text": text}]}

    mcp = {}
    if PUBLISH_PATH == "tool":  # in-process MCP server, so attribution is this runtime host's session
        mcp = {"robin": create_sdk_mcp_server(name="robin", version="0", tools=[finding_publish])}
    options = ClaudeAgentOptions(
        mcp_servers=mcp,
        allowed_tools=["mcp__robin__finding_publish"] if mcp else [],
        resume=RESUME_ID,
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
                if PUBLISH_PATH == "message" and isinstance(message, ResultMessage):
                    for block in parse_message_blocks(message.result or ""):
                        emit(submission_event("message", block, native_ref=message.uuid or uuid.uuid4().hex))

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


# -- Codex app-server runtime -------------------------------------------------

APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                    "execCommandApproval", "applyPatchApproval"}


class CodexRpc:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self, proc):
        self.proc = proc
        self.next_id = 0
        self.waiting: dict[int, asyncio.Future] = {}

    async def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiting[self.next_id] = fut
        await self._write({"id": self.next_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, 60)

    async def notify(self, method: str, params: dict | None = None):
        await self._write({"method": method, **({"params": params} if params else {})})

    async def respond(self, rid, result=None, error=None):
        await self._write({"id": rid, **({"error": error} if error else {"result": result})})

    async def _write(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()


async def run_codex(args):
    approvals = Approvals()
    proc = await asyncio.create_subprocess_exec(
        "codex", "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr, limit=MAX_LINE)
    rpc = CodexRpc(proc)
    state = {"thread": None, "turn": None}

    async def on_server_request(msg):
        method, params = msg["method"], msg.get("params", {})
        if method in APPROVAL_METHODS:
            decision = await approvals.request(method, params)
            await rpc.respond(msg["id"], {"decision": "accept" if decision.get("allow") else "decline"})
        elif method == "mcpServer/elicitation/request":  # e.g. MCP tool-call approval
            decision = await approvals.request(method, params)
            await rpc.respond(msg["id"], {"action": "accept", "content": {}} if decision.get("allow")
                              else {"action": "decline"})
        else:  # not handled by the spike; refuse explicitly rather than hang the turn
            emit({"type": "codex_unhandled_request", "method": method})
            await rpc.respond(msg["id"], error={"code": -32601, "message": "not supported by robin-spike"})

    async def pump():
        while line := await proc.stdout.readline():
            msg = json.loads(line)
            if "id" in msg and "method" not in msg:  # response to our call
                fut = rpc.waiting.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif "id" in msg:  # server -> client request
                asyncio.create_task(on_server_request(msg))
            else:  # notification
                params = msg.get("params", {})
                if msg["method"] == "turn/started":
                    state["turn"] = params.get("turn", {}).get("id") or params.get("turnId")
                elif msg["method"] == "turn/completed":
                    approvals.expire_all("turn ended")
                    state["turn"] = None
                emit({"type": "codex_notification", "method": msg["method"], "params": params})
                if msg["method"] == "item/completed":
                    codex_findings(params.get("item") or {})
        emit({"type": "codex_exited", "returncode": await proc.wait()})

    def codex_findings(item):
        if (PUBLISH_PATH == "tool" and item.get("type") == "mcpToolCall" and item.get("server") == "robin"
                and item.get("tool") == "finding_publish" and item.get("status") == "completed"):
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            emit(submission_event("tool", args, native_ref=item.get("id", "")))
        elif PUBLISH_PATH == "message" and item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
            for block in parse_message_blocks(item.get("text", "")):
                emit(submission_event("message", block, native_ref=item.get("id", "")))

    pump_task = asyncio.create_task(pump())
    init = await rpc.call("initialize", {"clientInfo": {"name": "robin-spike", "version": "0"}})
    await rpc.notify("initialized")
    emit({"type": "codex_initialized", "result": init.get("result"), "error": init.get("error"),
          "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN")})
    # Inherit the user's ~/.codex/config.toml; only ever request a stricter policy.
    start = {"cwd": args.cwd}
    if PUBLISH_PATH == "tool":  # Robin's MCP server for this thread only
        start["config"] = {"mcp_servers": {"robin": {
            "command": sys.executable,
            "args": [os.path.join(os.path.dirname(os.path.abspath(__file__)), "robin_mcp.py")]}}}
    if os.environ.get("ROBIN_CODEX_APPROVAL_POLICY"):
        start["approvalPolicy"] = os.environ["ROBIN_CODEX_APPROVAL_POLICY"]
    if RESUME_ID:
        start.pop("config", None)  # resume keeps the thread's own MCP config
        started = await rpc.call("thread/resume", {**start, "threadId": RESUME_ID})
    else:
        started = await rpc.call("thread/start", start)
    result = started.get("result") or {}
    state["thread"] = result.get("thread", {}).get("id")
    emit({"type": "session_started", "native_session_id": state["thread"],
          "approval_policy": result.get("approvalPolicy"), "sandbox": result.get("sandbox"),
          "model": result.get("model"), "error": started.get("error")})

    async for c in stdin_commands():
        if c["cmd"] == "send":
            text = [{"type": "text", "text": c["text"]}]
            if state["turn"]:
                r = await rpc.call("turn/steer", {"threadId": state["thread"], "expectedTurnId": state["turn"],
                                                  "input": text})
                emit({"type": "input_submitted", "mode": "steer", "error": r.get("error")})
            else:
                r = await rpc.call("turn/start", {"threadId": state["thread"], "input": text})
                emit({"type": "input_submitted", "mode": "new_turn", "error": r.get("error")})
        elif c["cmd"] == "interrupt":
            approvals.expire_all("interrupted")
            if state["turn"]:
                r = await rpc.call("turn/interrupt", {"threadId": state["thread"], "turnId": state["turn"]})
                emit({"type": "interrupt_submitted", "error": r.get("error")})
            else:
                emit({"type": "interrupt_submitted", "error": "no active turn"})
        elif c["cmd"] == "approval":
            approvals.answer(c["request_id"], c)
        elif c["cmd"] == "stop":
            break
    approvals.expire_all("session stopped")
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        proc.terminate()
    pump_task.cancel()
    emit({"type": "session_stopping"})


# -- Pi RPC runtime ---------------------------------------------------------------

PI_APPROVAL_EXTENSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_robin_approval.ts")


async def run_pi(args):
    approvals = Approvals()
    cmd = ["pi", "--mode", "rpc", "--no-approve", "-e", PI_APPROVAL_EXTENSION]
    if os.environ.get("ROBIN_PI_SESSION_DIR"):
        cmd += ["--session-dir", os.environ["ROBIN_PI_SESSION_DIR"]]
    if RESUME_ID:
        cmd += ["--session", RESUME_ID]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=args.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr, limit=MAX_LINE)
    state = {"busy": False, "next_id": 0}
    waiting: dict[str, asyncio.Future] = {}

    async def write(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def call(msg) -> dict:
        state["next_id"] += 1
        rid = f"robin-{state['next_id']}"
        fut = asyncio.get_running_loop().create_future()
        waiting[rid] = fut
        await write({**msg, "id": rid})
        return await asyncio.wait_for(fut, 60)

    async def on_ui_request(msg):
        if msg.get("method") == "confirm" and msg.get("title") == "robin:approval":
            detail = json.loads(msg.get("message") or "{}")
            decision = await approvals.request(detail.get("tool", "?"), detail.get("input", {}))
            await write({"type": "extension_ui_response", "id": msg["id"],
                         "confirmed": bool(decision.get("allow"))})
        elif msg.get("method") in ("select", "confirm", "input", "editor"):
            emit({"type": "pi_unhandled_dialog", "method": msg["method"], "title": msg.get("title")})
            await write({"type": "extension_ui_response", "id": msg["id"], "cancelled": True})
        else:  # fire-and-forget UI (notify, setStatus, ...): record, no reply
            emit({"type": "pi_ui", "method": msg.get("method")})

    def pi_findings(kind, msg):
        if PUBLISH_PATH == "tool" and kind == "tool_execution_start" and msg.get("toolName") == "robin_finding_publish":
            emit(submission_event("tool", msg.get("args"), native_ref=msg.get("toolCallId", "")))
        elif PUBLISH_PATH == "message" and kind == "message_end" and (msg.get("message") or {}).get("role") == "assistant":
            content = msg["message"].get("content")
            text = content if isinstance(content, str) else "".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")
            for block in parse_message_blocks(text):
                emit(submission_event("message", block, native_ref=str(msg["message"].get("timestamp", ""))))

    async def pump():
        while line := await proc.stdout.readline():  # splits on LF only, as Pi's framing requires
            msg = json.loads(line.rstrip(b"\r\n"))
            kind = msg.get("type")
            if kind == "response":
                fut = waiting.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
                emit({"type": "pi_response", "command": msg.get("command"), "success": msg.get("success"),
                      "error": msg.get("error")})
            elif kind == "extension_ui_request":
                asyncio.create_task(on_ui_request(msg))
            else:
                if kind == "agent_start":
                    state["busy"] = True
                elif kind == "agent_settled":
                    state["busy"] = False
                    approvals.expire_all("run settled")
                emit({"type": "pi_event", "event": kind, "data": msg})
                pi_findings(kind, msg)
        emit({"type": "pi_exited", "returncode": await proc.wait()})

    pump_task = asyncio.create_task(pump())
    st = await call({"type": "get_state"})
    data = st.get("data") or {}
    emit({"type": "session_started", "native_session_id": data.get("sessionId"),
          "session_file": data.get("sessionFile"), "model": (data.get("model") or {}).get("id"),
          "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN"), "error": st.get("error")})

    async for c in stdin_commands():
        if c["cmd"] == "send":
            mode = "steer" if state["busy"] else "new_turn"
            msg = {"type": "prompt", "message": c["text"]}
            if mode == "steer":
                msg["streamingBehavior"] = "steer"
            r = await call(msg)
            emit({"type": "input_submitted", "mode": mode,
                  "disposition": (r.get("data") or {}).get("disposition"), "error": r.get("error")})
        elif c["cmd"] == "interrupt":
            approvals.expire_all("interrupted")
            r = await call({"type": "abort"})
            emit({"type": "interrupt_submitted", "error": r.get("error")})
        elif c["cmd"] == "approval":
            approvals.answer(c["request_id"], c)
        elif c["cmd"] == "stop":
            break
    approvals.expire_all("session stopped")
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        proc.terminate()
    pump_task.cancel()
    emit({"type": "session_stopping"})


RUNTIMES = {"fake": run_fake, "claude": run_claude, "codex": run_codex, "pi": run_pi}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", choices=sorted(RUNTIMES), required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()
    asyncio.run(RUNTIMES[args.runtime](args))
