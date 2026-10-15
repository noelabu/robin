"""The single writer (TR-004): one thread owns the only write connection and runs short transactions.

A caller's future resolves only after COMMIT, so anything notified from that result is already durable.
Work items must be SQL only; file writes and fsyncs happen before submitting.
"""

import asyncio
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from sqlalchemy import Connection, Engine

type WorkFn[T] = Callable[[Connection], T]

_STOP = object()


class WriterBusy(Exception):
    """The write queue is full; slow the source down and retry."""


class WriterClosed(Exception):
    """The writer has shut down."""


class WriterThread:
    def __init__(self, engine: Engine, maxsize: int = 256):
        self._engine = engine
        self._queue: queue.Queue[Any] = queue.Queue(maxsize)
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="robin-writer", daemon=False)

    def start(self) -> None:
        self._thread.start()

    def submit[T](self, fn: WorkFn[T], *, block: bool = True, timeout: float | None = 5.0) -> Future[T]:
        fut: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise WriterClosed("writer is closed")
        try:
            self._queue.put((fn, fut), block=block, timeout=timeout)
        except queue.Full:
            raise WriterBusy("write queue is full") from None
        return fut

    def call[T](self, fn: WorkFn[T], timeout: float | None = 30.0) -> T:
        return self.submit(fn).result(timeout)

    async def acall[T](self, fn: WorkFn[T]) -> T:
        """For async callers: never blocks the event loop; a full queue raises WriterBusy at once."""
        fut = self.submit(fn, block=False)
        return await asyncio.wrap_future(fut)

    def close(self, timeout: float = 10.0) -> None:
        """Finish queued work, then stop. Later submits raise WriterClosed."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        with self._engine.connect() as conn:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                fn, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    with conn.begin():
                        result = fn(conn)
                except BaseException as exc:  # rolled back by the context manager; keep serving
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)
        self._engine.dispose()
