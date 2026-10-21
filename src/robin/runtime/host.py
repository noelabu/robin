"""A per-session runtime host: one child process of the supervisor per runtime session (TR-006).

Commands arrive as JSON lines on stdin and events leave as JSON lines on stdout, so the supervisor stays
runtime-agnostic. Runtimes register a coroutine in RUNTIMES; the scripted "fake" runtime here drives tests.

    python -m robin.runtime --runtime fake --cwd DIR

Commands: {"cmd": "send", "text": ..., "action_id": ...}, {"cmd": "interrupt", "action_id": ...},
          {"cmd": "approval", "request_id": ..., "allow": bool, "message": ..., "action_id": ...},
          the same with "answers": {question text: answer} for a question asked through `Approvals.ask`,
          {"cmd": "stop"}
"""

import argparse
import asyncio
import json
import os
import sys
import traceback
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from robin.findings.schema import parse_message_blocks, submission_event
from robin.supervisor.protocol import MAX_LINE

REPORTED: set[str] = set()  # holds "host_error" once one is out, so main() doesn't report a failure twice


def emit(event: dict[str, Any]) -> None:
    if event.get("type") == "host_error":
        REPORTED.add("host_error")
    sys.stdout.write(json.dumps(event, default=repr, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class StderrTail:
    """Passes a runtime child's stderr through to the host's own, keeping its end for an exit reason."""

    def __init__(self, stream: asyncio.StreamReader | None):
        self.tail = b""
        self.task = asyncio.create_task(self._relay(stream)) if stream else None

    async def _relay(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
            self.tail = (self.tail + chunk)[-2048:]

    async def exit_reason(self, what: str, returncode: int) -> str:
        if self.task:  # a grandchild may hold the pipe open: don't wait on it for long
            await asyncio.wait({self.task}, timeout=1)
        lines = self.tail.decode(errors="replace").strip().splitlines()
        return f"{what} exited ({returncode})" + (f": {lines[-1].strip()}" if lines else "")


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
        return await self._wait({"type": "approval_request", "tool": tool, "input": tool_input})

    async def ask(self, tool: str, tool_input: Any) -> dict[str, Any]:
        """A question for the user; the reply carries "answers" when they answered it."""
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        first = questions[0].get("question") if questions and isinstance(questions[0], dict) else None
        return await self._wait({"type": "question", "tool": tool, "input": tool_input, "text": first})

    async def _wait(self, event: dict[str, Any]) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        emit({**event, "request_id": rid})
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
                "answered": bool(decision.get("answers")),
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


def emit_message_findings(text: str, native_ref: str) -> int:
    """Publish the robin-finding blocks of an agent's final message (TR-067, message path)."""
    blocks = parse_message_blocks(text)
    for i, payload in enumerate(blocks):
        emit(submission_event("message", payload, f"{native_ref}:{i}"))
    return len(blocks)


FAKE_QUESTIONS = {
    "questions": [
        {
            "question": "Which database?",
            "header": "DB",
            "multiSelect": False,
            "options": [{"label": "SQLite", "description": ""}, {"label": "Postgres", "description": ""}],
        },
        {
            "question": "Which features?",
            "header": "Features",
            "multiSelect": True,
            "options": [{"label": "Auth"}, {"label": "Billing"}, {"label": "Search"}],
        },
    ]
}


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
            "native_session_id": os.environ.get("ROBIN_RESUME_NATIVE_ID") or uuid.uuid4().hex,
            "launch_token": os.environ.get("ROBIN_LAUNCH_TOKEN"),
            # What a real adapter would enforce or offer, so tests can see the launch env took effect.
            "deny_read": os.environ.get("ROBIN_DENY_READ"),
            "skip_permissions": os.environ.get("ROBIN_SKIP_PERMISSIONS"),
            "agent_tools": bool(os.environ.get("ROBIN_AGENT_TOKEN") and os.environ.get("ROBIN_SERVICE_URL")),
        }
    )
    turn: asyncio.Task[None] | None = None
    tick_s = float(os.environ.get("ROBIN_FAKE_TICK", "0.05"))

    async def work(text: str) -> None:
        emit({"type": "turn_started", "input": text})
        if text.startswith("<robin-context"):  # scripted reply to shared context
            sources = text.count("\n[source ")
            emit(
                {
                    "type": "message_completed",
                    "text": f"Synthesis of {sources} sources: they agree on the main point.",
                }
            )
            emit({"type": "turn_completed"})
            return
        if text.startswith("ask"):  # scripted question: two questions, one of them multi-select
            d = await approvals.ask("AskUserQuestion", FAKE_QUESTIONS)
            reply = (
                f"Answers: {json.dumps(d['answers'], sort_keys=True)}"
                if d.get("answers")
                else f"No answer: {d.get('message')}"
            )
            emit({"type": "message_completed", "text": reply})
            emit({"type": "turn_completed"})
            return
        if "```robin-finding" in text or text.startswith("tool-finding:"):
            # Scripted publication: a final message with finding blocks, or a finding tool call.
            ref = uuid.uuid4().hex
            if text.startswith("tool-finding:"):
                try:
                    payload = json.loads(text[len("tool-finding:") :])
                except ValueError as exc:
                    payload = {"_unparsable": str(exc)}
                emit(submission_event("tool", payload, ref))
            else:
                emit({"type": "message_completed", "text": text})
                emit_message_findings(text, ref)
            emit({"type": "turn_completed"})
            return
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
    try:
        asyncio.run(RUNTIMES[args.runtime](args))
    except Exception as exc:  # whatever the runtime didn't report itself (a missing binary, say): say why
        traceback.print_exc()
        if "host_error" not in REPORTED:
            reason = str(exc) if isinstance(exc, OSError) and str(exc) else repr(exc)
            emit({"type": "host_error", "reason": reason})
        raise SystemExit(1) from None


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
