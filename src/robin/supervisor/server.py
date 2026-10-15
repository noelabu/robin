"""The detached session supervisor (TR-006, TR-044, TR-072).

Owns one runtime-host child process per managed session, spools their events to disk, and serves them to
the Robin service over a user-only Unix socket. The service can exit or crash and reconnect with its
cursor; the hosts never notice. One supervisor per data directory, enforced with an OS lock.

    python -m robin.supervisor.server --data DIR      # foreground
    robin.supervisor.server.start_detached(DIR)       # detached, for the service
"""

import argparse
import asyncio
import faulthandler
import fcntl
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from robin.paths import secure_mkdir, secure_touch
from robin.supervisor.protocol import MAX_LINE, PROTOCOL_VERSION, decode, encode
from robin.supervisor.spool import Spool

SOCKET_LIMIT = 100  # macOS sun_path is 104 bytes


def supervisor_dir(data_dir: Path) -> Path:
    return data_dir / "supervisor"


def socket_path(data_dir: Path) -> Path:
    preferred = supervisor_dir(data_dir) / "supervisor.sock"
    if len(str(preferred)) < SOCKET_LIMIT:
        return preferred
    digest = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
    return _private_tmp() / f"{digest}.sock"


def _private_tmp() -> Path:
    """/tmp/robin-<uid>, mode 0700 and owned by us, so no other user can plant or reach the socket."""
    d = Path("/tmp") / f"robin-{os.getuid()}"
    try:
        d.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if st.st_uid != os.getuid() or not os.path.isdir(d) or os.path.islink(d):
        raise PermissionError(f"{d} is not a directory owned by this user")
    if st.st_mode & 0o777 != 0o700:
        d.chmod(0o700)
    return d


class Supervisor:
    def __init__(self, data_dir: Path):
        self.dir = secure_mkdir(supervisor_dir(data_dir))
        self.generation = uuid.uuid4().hex[:12]
        self.spool = Spool(self.dir / "spool")
        self.sessions: dict[str, dict[str, Any]] = {}
        self.writer: asyncio.StreamWriter | None = None
        self.connections: set[asyncio.StreamWriter] = set()
        self.stopping = asyncio.Event()

    # -- events ---------------------------------------------------------

    async def emit(self, session: str, event: dict[str, Any]) -> None:
        rec = self.spool.append({"gen": self.generation, "session": session, "at": time.time(), "event": event})
        await self._send({"type": "event", **rec})

    async def _send(self, msg: dict[str, Any]) -> None:
        w = self.writer
        if w is None or w.is_closing():
            return
        try:
            w.write(encode(msg))
            await w.drain()
        except (ConnectionError, OSError):
            self.writer = None  # the service went away; events stay in the spool

    # -- runtime hosts --------------------------------------------------

    async def launch(self, req: dict[str, Any]) -> dict[str, Any]:
        session = req.get("session")
        if not session:
            return {"ok": False, "error": "launch needs the Robin session id"}
        if session in self.sessions and self.sessions[session]["alive"]:
            return {"ok": False, "error": "session_already_running"}
        token = secrets.token_hex(8)
        env = {**os.environ, "ROBIN_LAUNCH_TOKEN": token, **(req.get("env") or {})}
        with open(self.dir / f"host-{session}.log", "ab") as log:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "robin.runtime", "--runtime", req["runtime"],
                "--cwd", req.get("cwd") or os.getcwd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env, limit=MAX_LINE,
                cwd=req.get("cwd") or None)
        self.sessions[session] = {"proc": proc, "runtime": req["runtime"], "pid": proc.pid,
                                  "launch_token": token, "started": time.time(), "alive": True}
        await self.emit(session, {"type": "host_spawned", "pid": proc.pid, "runtime": req["runtime"],
                                  "launch_token": token, "supervisor_generation": self.generation})
        asyncio.create_task(self._pump(session, proc))
        if req.get("prompt"):
            await self._to_host(session, {"cmd": "send", "text": req["prompt"], "action_id": req.get("action_id")})
        return {"ok": True, "session": session, "pid": proc.pid, "launch_token": token}

    async def _pump(self, session: str, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # a line over MAX_LINE: report it, keep reading
                await self.emit(session, {"type": "host_oversized_output"})
                continue
            if not line:
                break
            try:
                event = decode(line)
            except ValueError:
                event = {"type": "host_malformed_output", "bytes": len(line)}
            await self.emit(session, event)
        rc = await proc.wait()
        self.sessions[session]["alive"] = False
        await self.emit(session, {"type": "host_exited", "returncode": rc})

    async def _to_host(self, session: str, command: dict[str, Any]) -> dict[str, Any]:
        s = self.sessions.get(session)
        if not s or not s["alive"]:
            return {"ok": False, "error": "no_such_live_session"}
        proc: asyncio.subprocess.Process = s["proc"]
        assert proc.stdin is not None
        try:
            proc.stdin.write(encode(command))
            await proc.stdin.drain()
        except (ConnectionError, OSError):
            return {"ok": False, "error": "host_not_accepting_input"}
        return {"ok": True}

    def summary(self) -> dict[str, Any]:
        return {sid: {k: v for k, v in s.items() if k != "proc"} for sid, s in self.sessions.items()}

    # -- service connection ---------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.add(writer)
        try:
            await self._serve_client(reader, writer)
        finally:
            self.connections.discard(writer)
            if self.writer is writer:
                self.writer = None
            writer.close()

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = decode(await reader.readline())
        except ValueError:
            return
        if hello.get("type") == "probe":  # read-only status for diagnostics: never becomes the service
            writer.write(encode({"type": "probe_ok", "protocol": PROTOCOL_VERSION, "generation": self.generation,
                                 "sessions": self.summary(), "head": self.spool.seq, "acked": self.spool.acked}))
            await writer.drain()
            return
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
            writer.write(encode({"type": "hello_rejected", "protocol": PROTOCOL_VERSION}))
            await writer.drain()
            return
        if self.writer and not self.writer.is_closing():
            self.writer.close()  # one service at a time; the newest connection wins
        last = int(hello.get("after", 0))
        writer.write(encode({"type": "hello_ok", "protocol": PROTOCOL_VERSION, "generation": self.generation,
                             "head": self.spool.seq, "acked": self.spool.acked,
                             "dropped": self.spool.dropped, "sessions": self.summary()}))
        # Replay until caught up, then register with no await in between: nothing lost, nothing reordered.
        while backlog := self.spool.after(last):
            for rec in backlog:
                writer.write(encode({"type": "event", **rec}))
                last = rec["seq"]
            await writer.drain()
        self.writer = writer
        while line := await reader.readline():
            try:
                msg = decode(line)
            except ValueError:
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        kind, corr = msg.get("type"), msg.get("corr")
        try:
            if kind == "ack":
                self.spool.ack(int(msg["seq"]))
                return
            result = await self._handle_request(kind, msg)
        except Exception as exc:  # a bad request gets an error reply; the connection stays up
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        await self._send({"type": "reply", "corr": corr, **result})

    async def _handle_request(self, kind: Any, msg: dict[str, Any]) -> dict[str, Any]:
        if kind == "launch":
            result = await self.launch(msg)
        elif kind == "command":
            result = await self._to_host(msg["session"], msg["command"])
        elif kind == "status":
            result = {"ok": True, "generation": self.generation, "protocol": PROTOCOL_VERSION,
                      "sessions": self.summary(), "spool_bytes": self.spool.total_bytes(),
                      "head": self.spool.seq, "acked": self.spool.acked}
        elif kind == "shutdown":
            result = self._shutdown(msg)
        else:
            result = {"ok": False, "error": f"unknown message type {kind!r}"}
        return result

    def _shutdown(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Stopping the supervisor ends its sessions, so the request must name exactly those (TR-072)."""
        alive = sorted(s for s, v in self.sessions.items() if v["alive"])
        if sorted(msg.get("confirm_sessions") or []) != alive:
            return {"ok": False, "error": "confirm_sessions_mismatch", "would_end": alive}
        self.stopping.set()
        return {"ok": True, "ending": alive}

    async def serve(self, sock: Path) -> None:
        if sock.exists() or sock.is_symlink():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle, path=str(sock), limit=MAX_LINE)
        os.chmod(sock, 0o600)
        await self.stopping.wait()
        server.close()
        for w in list(self.connections):
            w.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 5)
        except TimeoutError:
            pass
        for s in self.sessions.values():
            if s["alive"]:
                s["proc"].terminate()
                await s["proc"].wait()
        self.spool.close()
        sock.unlink(missing_ok=True)


def serve(data_dir: Path) -> None:
    sup_dir = secure_mkdir(supervisor_dir(data_dir))
    lock_path = secure_touch(sup_dir / "supervisor.lock")
    lock = open(lock_path, "w")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another supervisor owns this data directory")
    lock.write(str(os.getpid()))
    lock.flush()
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # kill -USR1 <pid> dumps stacks to the log
    asyncio.run(Supervisor(data_dir).serve(socket_path(data_dir)))


def start_detached(data_dir: Path) -> int:
    """Start a supervisor in its own session so it outlives the service. Returns its pid."""
    sup_dir = secure_mkdir(supervisor_dir(data_dir))
    log = open(sup_dir / "supervisor.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "robin.supervisor.server", "--data", str(data_dir)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()
    return proc.pid


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="robin-supervisor")
    ap.add_argument("--data", type=Path, required=True)
    serve(ap.parse_args().data.expanduser().resolve())
