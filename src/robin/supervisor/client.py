"""The service's connection to the supervisor, and the bridge that ingests its stream (TR-006, TR-004)."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from robin.events.supervisor_source import SupervisorIngestor, SupervisorRecord
from robin.store import Store
from robin.store.models import SourceCheckpoint
from robin.supervisor.protocol import MAX_LINE, PROTOCOL_VERSION, decode, encode
from robin.supervisor.server import socket_path, start_detached


class SupervisorUnavailable(RuntimeError):
    pass


class SupervisorClient:
    def __init__(self, data_dir: Path):
        self.sock = socket_path(data_dir)
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.hello: dict[str, Any] = {}
        self._replies: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    async def connect(self, after: int) -> dict[str, Any]:
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self.sock), limit=MAX_LINE)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise SupervisorUnavailable(str(exc)) from exc
        self._writer.write(encode({"type": "hello", "protocol": PROTOCOL_VERSION, "after": after}))
        await self._writer.drain()
        self.hello = decode(await self._reader.readline())
        if self.hello.get("type") != "hello_ok":
            raise SupervisorUnavailable(f"supervisor refused the connection: {self.hello}")
        self._reader_task = asyncio.create_task(self._read())
        return self.hello

    async def _read(self) -> None:
        try:
            while line := await self._reader.readline():
                msg = decode(line)
                if msg.get("type") == "event":
                    await self.events.put(msg)
                elif msg.get("type") == "reply" and msg.get("corr") in self._replies:
                    self._replies.pop(msg["corr"]).set_result(msg)
        except (ConnectionError, ValueError):
            pass
        finally:
            for fut in self._replies.values():
                if not fut.done():
                    fut.set_exception(SupervisorUnavailable("connection lost"))
            self.closed.set()

    async def request(self, msg: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        corr = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[corr] = fut
        self._writer.write(encode({**msg, "corr": corr}))
        await self._writer.drain()
        return await asyncio.wait_for(fut, timeout)

    async def ack(self, seq: int) -> None:
        self._writer.write(encode({"type": "ack", "seq": seq}))
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        if self._reader_task:
            await asyncio.wait([self._reader_task], timeout=5)


async def ensure_supervisor(data_dir: Path, timeout: float = 10.0) -> None:
    """Start a detached supervisor unless one is already serving this data directory."""
    sock = socket_path(data_dir)
    if await _answers(sock):
        return
    start_detached(data_dir)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _answers(sock):
            return
        await asyncio.sleep(0.05)
    raise SupervisorUnavailable(f"supervisor did not start within {timeout}s")


async def _answers(sock: Path) -> bool:
    try:
        _, w = await asyncio.open_unix_connection(str(sock))
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    w.close()
    return True


def committed_cursor(store: Store) -> int:
    """The highest supervisor seq Robin has committed. seq is global across generations."""
    with store.reader.connect() as conn:
        value = conn.execute(select(func.max(SourceCheckpoint.offset)).where(
            SourceCheckpoint.kind == "supervisor")).scalar()
    return int(value or 0)


class SupervisorBridge:
    """Ingests the supervisor stream and acknowledges only what is committed."""

    def __init__(self, store: Store, client: SupervisorClient, ingestor: SupervisorIngestor | None = None,
                 batch: int = 200):
        self.store = store
        self.client = client
        self.ingestor = ingestor or SupervisorIngestor(store)
        self.batch = batch
        self.cursor = committed_cursor(store)

    async def run(self) -> None:
        while not (self.client.closed.is_set() and self.client.events.empty()):
            get = asyncio.create_task(self.client.events.get())
            closed = asyncio.create_task(self.client.closed.wait())
            done, _ = await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
            if get not in done:
                get.cancel()
                closed.cancel()
                continue
            closed.cancel()
            records = [get.result()]
            while len(records) < self.batch and not self.client.events.empty():
                records.append(self.client.events.get_nowait())
            await self.ingest(records)

    async def ingest(self, raw: list[dict[str, Any]]) -> None:
        fresh = [SupervisorRecord(**{k: r[k] for k in ("seq", "gen", "session", "at", "event")})
                 for r in raw if r["seq"] > self.cursor]
        if fresh:
            await asyncio.to_thread(self.ingestor.ingest, fresh)
            self.cursor = max(self.cursor, max(r.seq for r in fresh))
        if not self.client.closed.is_set():
            await self.client.ack(self.cursor)  # after commit: the supervisor may now drop these
