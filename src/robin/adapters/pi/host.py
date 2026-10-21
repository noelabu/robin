"""The Pi runtime host, through `pi --mode rpc` (Phase 0: pi/findings.md).

Pi has no built-in approvals; Robin's extension asks before calls that may change files or reach the
network (on by default, ROBIN_PI_APPROVALS=0 turns it off). Input sent during a run is queued by Pi until
the turn boundary, so Robin labels it "queued". Pi has no sandbox: review independence is not enforced.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from robin.adapters.pi.translate import translate
from robin.runtime.host import Approvals, StderrTail, emit, runtime, stdin_commands
from robin.supervisor.protocol import MAX_LINE

EXTENSION = Path(__file__).resolve().parent / "robin_extension.ts"


@runtime("pi")
async def run_pi(args: argparse.Namespace) -> None:
    approvals = Approvals()
    cmd = ["pi", "--mode", "rpc", "--no-approve", "-e", str(EXTENSION)]
    if os.environ.get("ROBIN_PI_SESSION_DIR"):
        cmd += ["--session-dir", os.environ["ROBIN_PI_SESSION_DIR"]]
    if os.environ.get("ROBIN_RESUME_NATIVE_ID"):
        cmd += ["--session", os.environ["ROBIN_RESUME_NATIVE_ID"]]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=args.cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
    )
    stderr = StderrTail(proc.stderr)
    state: dict[str, Any] = {"busy": False, "next": 0, "closed": None}
    waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def write(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def call(msg: dict[str, Any]) -> dict[str, Any]:
        if state["closed"]:
            raise ConnectionError(state["closed"])
        state["next"] += 1
        rid = f"robin-{state['next']}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiting[rid] = fut
        try:
            await write({**msg, "id": rid})
        except ConnectionError:  # pi is gone; the pump fails the call with the reason
            pass
        try:
            return await asyncio.wait_for(fut, 60)
        except TimeoutError:
            raise TimeoutError(f"pi didn't answer {msg.get('type')} within 60s") from None

    async def ui(msg: dict[str, Any]) -> None:
        if msg.get("method") == "confirm" and msg.get("title") == "robin:approval":
            detail = json.loads(msg.get("message") or "{}")
            decision = await approvals.request(detail.get("tool", "?"), detail.get("input", {}))
            await write(
                {"type": "extension_ui_response", "id": msg["id"], "confirmed": bool(decision.get("allow"))}
            )
        elif msg.get("method") in ("select", "confirm", "input", "editor"):
            emit({"type": "pi_unhandled_dialog", "method": msg.get("method"), "title": msg.get("title")})
            await write({"type": "extension_ui_response", "id": msg["id"], "cancelled": True})

    async def pump() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():  # LF-only split, as Pi's framing requires
            msg = json.loads(line.rstrip(b"\r\n"))
            kind = msg.get("type")
            if kind == "response":
                fut = waiting.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif kind == "extension_ui_request":
                asyncio.create_task(ui(msg))
            else:
                for event in translate(msg):
                    if event["type"] == "turn_started":
                        state["busy"] = True
                    elif event["type"] in ("turn_completed", "turn_interrupted"):
                        state["busy"] = False
                        approvals.expire_all("turn ended")
                    emit(event)
        state["closed"] = reason = await stderr.exit_reason("pi", await proc.wait())
        for fut in waiting.values():  # calls still waiting fail now, rather than at their timeout
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        waiting.clear()
        emit({"type": "host_error", "reason": reason})

    pump_task = asyncio.create_task(pump())
    st = await call({"type": "get_state"})
    data = st.get("data") or {}
    if not st.get("success"):
        emit({"type": "host_error", "reason": f"get_state failed: {st.get('error')}"})
        raise SystemExit(2)
    emit(
        {
            "type": "session_started",
            "native_session_id": data.get("sessionId"),
            "session_file": data.get("sessionFile"),
            "model": (data.get("model") or {}).get("id"),
            "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN"),
        }
    )

    async def commands() -> None:
        async for c in stdin_commands():
            action_id = c.get("action_id")
            try:
                match c.get("cmd"):
                    case "send":
                        busy = state["busy"]
                        msg = {"type": "prompt", "message": c.get("text", "")}
                        if busy:
                            msg["streamingBehavior"] = "steer"
                        r = await call(msg)
                        if r.get("success"):
                            emit(
                                {
                                    "type": "input_submitted",
                                    "mode": "queued" if busy else "new_turn",
                                    "action_id": action_id,
                                }
                            )
                        else:
                            emit(
                                {
                                    "type": "command_failed",
                                    "cmd": "send",
                                    "reason": str(r.get("error")),
                                    "action_id": action_id,
                                }
                            )
                    case "interrupt":
                        approvals.expire_all("interrupted")
                        r = await call({"type": "abort"})
                        if r.get("success"):
                            emit({"type": "interrupt_acknowledged", "action_id": action_id})
                        else:
                            emit(
                                {
                                    "type": "command_failed",
                                    "cmd": "interrupt",
                                    "reason": str(r.get("error")),
                                    "action_id": action_id,
                                }
                            )
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
