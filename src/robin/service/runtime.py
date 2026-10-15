"""The service's long-lived parts: store, supervisor link, runtime manager, change notification.

The supervisor link reconnects on its own. While it is down, connected sessions show as stale; on
reconnect the manager reconciles from what the supervisor reports and the bridge resumes from the
committed cursor, so nothing is duplicated or relaunched (TR-036, TR-044).
"""

import asyncio
import logging
from contextlib import suppress

from robin.events.supervisor_source import SupervisorRecord
from robin.runtime.manager import RuntimeManager
from robin.store import Store
from robin.supervisor.client import (
    SupervisorBridge,
    SupervisorClient,
    SupervisorUnavailable,
    committed_cursor,
    ensure_supervisor,
)

log = logging.getLogger(__name__)


class ServiceRuntime:
    def __init__(self, store: Store, *, start_supervisor: bool = True):
        self.store = store
        self.manager = RuntimeManager(store)
        self.start_supervisor = start_supervisor
        self.changed = asyncio.Condition()
        self.version = 0  # bumped on every change; waiters compare against what they last saw
        self.supervisor_connected = False
        self._task: asyncio.Task[None] | None = None
        self._indexer: asyncio.Task[None] | None = None
        self._client: SupervisorClient | None = None

    async def start(self) -> None:
        await self.manager.recover()
        self._task = asyncio.create_task(self._link())
        self._indexer = asyncio.create_task(self._index())

    async def stop(self) -> None:
        for task in (self._task, self._indexer):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self._client:
            await self._client.close()

    async def notify(self) -> None:
        async with self.changed:
            self.version += 1
            self.changed.notify_all()

    async def wait_change(self, seen: int, timeout: float) -> int:
        """Return as soon as the version differs from `seen`, including a change that already happened."""
        async with self.changed:
            try:
                await asyncio.wait_for(self.changed.wait_for(lambda: self.version != seen), timeout)
            except TimeoutError:
                pass
            return self.version

    async def _index(self) -> None:
        """Keep the search index caught up, off the request path (TR-081)."""
        from robin import search

        seen = -1
        while True:
            try:
                await asyncio.to_thread(search.catch_up, self.store)
            except Exception:
                log.exception("search indexing failed; retrying")
            seen = await self.wait_change(seen, 5.0)

    async def _on_committed(self, records: list[SupervisorRecord]) -> None:
        await self.manager.on_committed(records)
        await self.notify()  # after commit and projection

    async def _link(self) -> None:
        delay = 0.2
        while True:
            try:
                if self.start_supervisor:
                    await ensure_supervisor(self.store.data_dir)
                client = SupervisorClient(self.store.data_dir)
                await client.connect(after=committed_cursor(self.store))
            except (SupervisorUnavailable, OSError) as exc:
                log.warning("supervisor unavailable: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            delay = 0.2
            self._client = client
            try:
                await self.manager.attach(client)
                self.supervisor_connected = True
                await self.notify()
                await SupervisorBridge(self.store, client, on_committed=self._on_committed).run()
            except asyncio.CancelledError:
                raise
            except Exception:  # e.g. a database error during ingest: log, back off, reconnect
                log.exception("supervisor link failed; reconnecting")
                await asyncio.sleep(delay)
            finally:
                self.supervisor_connected = False
                self.manager.client = None
                await client.close()
                await self.manager.connection_lost()
                await self.notify()
