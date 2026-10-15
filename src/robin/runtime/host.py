"""A per-session runtime host: one child process of the supervisor per runtime session (TR-006).

Commands arrive as JSON lines on stdin and events leave as JSON lines on stdout, so the supervisor stays
runtime-agnostic. Runtimes register a coroutine in RUNTIMES; the scripted "fake" runtime here drives tests.

    python -m robin.runtime --runtime fake --cwd DIR

Commands: {"cmd": "send", "text": ..., "action_id": ...}, {"cmd": "interrupt", "action_id": ...},
          {"cmd": "approval", "request_id": ..., "allow": bool, "message": ..., "action_id": ...},
          {"cmd": "stop"}
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from robin.supervisor.protocol import MAX_LINE


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, default=repr, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def stdin_commands() -> AsyncIterator[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        try:
            yield json.loads(line)
        except ValueError:
            emit({"type": "command_rejected", "reason": "invalid_json"})


class Approvals:
    """Pending approval requests. Without native expiry they expire when the run ends or is interrupted
    (TR-045), and a reply to an expired or answered request is rejected, never applied."""

    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def request(self, tool: str, tool_input: Any) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        emit({"type": "approval_request", "request_id": rid, "tool": tool, "input": tool_input})
        try:
            return await fut
        finally:
            self.pending.pop(rid, None)

    def answer(self, rid: str, decision: dict[str, Any]) -> None:
        fut = self.pending.get(rid)
        if fut is None or fut.done():
            emit(
                {
                    "type": "approval_rejected",
                    "request_id": rid,
                    "reason": "unknown_or_expired",
                    "action_id": decision.get("action_id"),
                }
            )
            return
        fut.set_result(decision)
        emit(
            {
                "type": "approval_answered",
                "request_id": rid,
                "allow": bool(decision.get("allow")),
                "action_id": decision.get("action_id"),
            }
        )

    def expire_all(self, reason: str) -> None:
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_result({"allow": False, "message": f"Request expired: {reason}", "expired": True})
                emit({"type": "approval_expired", "request_id": rid, "reason": reason})


type RuntimeMain = Callable[[argparse.Namespace], Coroutine[Any, Any, None]]
RUNTIMES: dict[str, RuntimeMain] = {}


def runtime(name: str) -> Callable[[RuntimeMain], RuntimeMain]:
    def register(fn: RuntimeMain) -> RuntimeMain:
        RUNTIMES[name] = fn
        return fn

    return register


@runtime("fake")
async def run_fake(args: argparse.Namespace) -> None:
    """Scripted runtime: ticks, one approval at tick 5, interrupt and stop. No network."""
    approvals = Approvals()
    if os.environ.get("ROBIN_FAKE_FAIL_START"):
        emit({"type": "host_error", "reason": "simulated start failure"})
        raise SystemExit(3)
    emit(
        {
            "type": "session_started",
            "native_session_id": uuid.uuid4().hex,
            "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN"),
        }
    )
    turn: asyncio.Task[None] | None = None
    tick_s = float(os.environ.get("ROBIN_FAKE_TICK", "0.05"))

    async def work(text: str) -> None:
        emit({"type": "turn_started", "input": text})
        try:
            for n in range(1, 1_000_000):
                await asyncio.sleep(tick_s)
                emit({"type": "tick", "n": n})
                if n == 5:
                    decision = await approvals.request("Bash", {"command": "make test"})
                    emit({"type": "tool_decision", "allow": bool(decision.get("allow"))})
        except asyncio.CancelledError:
            emit({"type": "turn_interrupted"})
            raise

    async for c in stdin_commands():
        match c.get("cmd"):
            case "send":
                if turn and not turn.done():
                    emit({"type": "input_submitted", "mode": "queued", "action_id": c.get("action_id")})
                else:
                    turn = asyncio.create_task(work(c.get("text", "")))
                    emit({"type": "input_submitted", "mode": "new_turn", "action_id": c.get("action_id")})
            case "interrupt":
                approvals.expire_all("interrupted")
                if turn:
                    turn.cancel()
                emit({"type": "interrupt_acknowledged", "action_id": c.get("action_id")})
            case "approval":
                approvals.answer(c.get("request_id", ""), c)
            case "stop":
                break
            case other:
                emit({"type": "command_rejected", "reason": f"unknown command {other!r}"})
    approvals.expire_all("session stopped")
    if turn:
        turn.cancel()
    emit({"type": "session_stopping"})


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="robin-runtime-host")
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)
    if args.runtime not in RUNTIMES:
        _load_adapter(args.runtime)
    if args.runtime not in RUNTIMES:
        emit({"type": "host_error", "reason": f"unknown runtime {args.runtime!r}"})
        raise SystemExit(2)
    asyncio.run(RUNTIMES[args.runtime](args))


def _load_adapter(name: str) -> None:
    """Import only this runtime's adapter, so a broken adapter can't affect the others (NFR-06)."""
    import importlib

    if name.isidentifier():
        try:
            importlib.import_module(f"robin.adapters.{name}.host")
        except ModuleNotFoundError:
            pass


if __name__ == "__main__":
    main()
