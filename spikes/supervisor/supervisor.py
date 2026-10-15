"""Phase 0 spike: a detached session supervisor (TR-006).

Owns runtime-host child processes, spools their events to disk with a
supervisor sequence number, and replays unacknowledged events to the
service over a user-only Unix socket. The service can die and reconnect
without the runtime hosts noticing.

    python supervisor.py start --data DIR   # detach and serve
    python supervisor.py serve --data DIR   # serve in the foreground
"""

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

from protocol import MAX_LINE, PROTOCOL_VERSION, decode, encode

HERE = Path(__file__).resolve().parent


class Supervisor:
    def __init__(self, data: Path):
        self.data = data
        self.generation = uuid.uuid4().hex[:12]
        self.spool_path = data / "spool.jsonl"
        self.seq = 0
        self.spool: list[dict] = []
        self.acked = 0
        self.sessions: dict[str, dict] = {}
        self.writer: asyncio.StreamWriter | None = None
        self.stopping = asyncio.Event()
        self._load_spool()
        self.spool_file = open(self.spool_path, "a")

    def _load_spool(self):
        if self.spool_path.exists():
            for line in self.spool_path.read_text().splitlines():
                rec = json.loads(line)
                self.spool.append(rec)
                self.seq = rec["seq"]

    # -- events ---------------------------------------------------------

    async def emit(self, session: str, event: dict):
        self.seq += 1
        rec = {"seq": self.seq, "gen": self.generation, "session": session,
               "at": time.time(), "event": event}
        self.spool.append(rec)
        self.spool_file.write(json.dumps(rec, default=str) + "\n")
        self.spool_file.flush()
        os.fsync(self.spool_file.fileno())
        await self._send({"type": "event", **rec})

    async def _send(self, msg: dict):
        w = self.writer
        if w is None or w.is_closing():
            return
        try:
            w.write(encode(msg))
            await w.drain()
        except (ConnectionError, OSError):
            self.writer = None  # service went away; events stay in the spool

    # -- runtime hosts --------------------------------------------------

    async def launch(self, req: dict) -> dict:
        session = req.get("session") or uuid.uuid4().hex[:12]
        if session in self.sessions and self.sessions[session]["alive"]:
            return {"ok": False, "error": "session_already_running"}
        token = secrets.token_hex(8)
        cmd = [sys.executable, str(HERE / "runtime_host.py"),
               "--runtime", req["runtime"], "--cwd", req.get("cwd", os.getcwd())]
        env = {**os.environ, "ROBIN_LAUNCH_TOKEN": token}
        env.update(req.get("env", {}))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.data / f"host-{session}.log", "ab"),
            env=env, limit=MAX_LINE)
        self.sessions[session] = {"proc": proc, "runtime": req["runtime"], "pid": proc.pid,
                                  "token": token, "started": time.time(), "alive": True}
        await self.emit(session, {"type": "host_spawned", "pid": proc.pid,
                                  "runtime": req["runtime"], "launch_token": token})
        asyncio.create_task(self._pump(session, proc))
        if req.get("prompt"):
            await self._to_host(session, {"cmd": "send", "text": req["prompt"]})
        return {"ok": True, "session": session, "pid": proc.pid}

    async def _pump(self, session: str, proc: asyncio.subprocess.Process):
        while line := await proc.stdout.readline():
            try:
                event = decode(line)
            except ValueError:
                event = {"type": "host_malformed_output", "bytes": len(line)}
            await self.emit(session, event)
        rc = await proc.wait()
        self.sessions[session]["alive"] = False
        await self.emit(session, {"type": "host_exited", "returncode": rc})

    async def _to_host(self, session: str, command: dict) -> dict:
        s = self.sessions.get(session)
        if not s or not s["alive"]:
            return {"ok": False, "error": "no_such_live_session"}
        s["proc"].stdin.write(encode(command))
        await s["proc"].stdin.drain()
        return {"ok": True}

    # -- service connection ---------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            hello = decode(await reader.readline())
        except ValueError:
            writer.close()
            return
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
            writer.write(encode({"type": "hello_rejected", "protocol": PROTOCOL_VERSION}))
            writer.close()
            return
        if self.writer and not self.writer.is_closing():
            self.writer.close()  # one service at a time; newest wins
        last = int(hello.get("after", 0))
        writer.write(encode({"type": "hello_ok", "protocol": PROTOCOL_VERSION,
                             "generation": self.generation, "head": self.seq,
                             "sessions": self._session_summary()}))
        # Replay until caught up, then register the writer with no await in between,
        # so events emitted during replay are neither lost nor sent out of order.
        while backlog := [r for r in self.spool if r["seq"] > last]:
            for rec in backlog:
                writer.write(encode({"type": "event", **rec}))
                last = rec["seq"]
            await writer.drain()
        self.writer = writer
        try:
            while line := await reader.readline():
                await self._dispatch(decode(line))
        except (ConnectionError, ValueError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None

    def _session_summary(self):
        return {sid: {k: v for k, v in s.items() if k != "proc"} for sid, s in self.sessions.items()}

    async def _dispatch(self, msg: dict):
        kind, corr = msg.get("type"), msg.get("corr")
        if kind == "ack":
            self.acked = max(self.acked, int(msg["seq"]))
            return
        if kind == "launch":
            result = await self.launch(msg)
        elif kind == "command":
            result = await self._to_host(msg["session"], msg["command"])
        elif kind == "status":
            result = {"ok": True, "sessions": self._session_summary(), "acked": self.acked}
        elif kind == "shutdown":
            result = {"ok": True, "ending": [s for s, v in self.sessions.items() if v["alive"]]}
            self.stopping.set()
        else:
            result = {"ok": False, "error": f"unknown message type {kind!r}"}
        await self._send({"type": "reply", "corr": corr, **result})

    async def serve(self, sock: Path):
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle, path=str(sock), limit=MAX_LINE)
        os.chmod(sock, 0o600)
        async with server:
            await self.stopping.wait()
        for s in self.sessions.values():
            if s["alive"]:
                s["proc"].terminate()
                await s["proc"].wait()


def serve(data: Path):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(data / "supervisor.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another supervisor owns this data directory")
    lock.write(str(os.getpid()))
    lock.flush()
    asyncio.run(Supervisor(data).serve(data / "supervisor.sock"))


def start(data: Path):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open(data / "supervisor.log", "ab")
    proc = subprocess.Popen([sys.executable, __file__, "serve", "--data", str(data)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True, cwd=HERE)
    print(proc.pid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "serve"])
    ap.add_argument("--data", type=Path, required=True)
    args = ap.parse_args()
    (start if args.action == "start" else serve)(args.data.resolve())
