"""The service's long-lived parts: store, supervisor link, runtime manager, change notification.

The supervisor link reconnects on its own. While it is down, connected sessions show as stale; on
reconnect the manager reconciles from what the supervisor reports and the bridge resumes from the
committed cursor, so nothing is duplicated or relaunched (TR-036, TR-044).
"""

import asyncio
import logging
import threading
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
CAP_CHECK_S = 600.0


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
        self._watcher: asyncio.Task[None] | None = None
        self._watch_stop = threading.Event()
        self._client: SupervisorClient | None = None

    async def start(self) -> None:
        await self.manager.recover()
        self._task = asyncio.create_task(self._link())
        self._indexer = asyncio.create_task(self._index())
        self._watcher = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        self._watch_stop.set()
        for task in (self._task, self._indexer, self._watcher):
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
        from robin import retention, search

        seen = -1
        last_cap_check = 0.0
        while True:
            try:
                await asyncio.to_thread(search.catch_up, self.store)
            except Exception:
                log.exception("search indexing failed; retrying")
            now = asyncio.get_running_loop().time()
            if now - last_cap_check > CAP_CHECK_S:  # oldest-first pruning above the cap (TR-081)
                last_cap_check = now
                try:
                    done = await asyncio.to_thread(retention.enforce, self.store)
                    if done:
                        log.warning("storage cap reached: pruned %d oldest events", done["pruned_events"])
                        await self.notify()
                except Exception:
                    log.exception("storage cap check failed")
            seen = await self.wait_change(seen, 5.0)

    async def _watch(self) -> None:
        """Follow native history while the user has it turned on (`robin watch on`)."""
        from robin import watch

        watcher = watch.HistoryWatcher(self.store)
        waker = watch.Waker()
        try:
            while not self._watch_stop.is_set():
                if not await asyncio.to_thread(watch.enabled, self.store):
                    await self.wait_change(self.version, 5.0)
                    continue
                try:
                    step = await asyncio.to_thread(
                        watch.run_once, watcher, waker, watch.POLL_S, self._watch_stop
                    )
                    if step["changed_files"]:
                        await self.notify()
                except Exception:
                    log.exception("history watch failed; retrying")
                    await asyncio.sleep(5)
        finally:
            waker.close()

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
