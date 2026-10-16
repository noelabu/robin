"""The service's long-lived parts: store, supervisor link, runtime manager, change notification.

The supervisor link reconnects on its own. While it is down, connected sessions show as stale; on
reconnect the manager reconciles from what the supervisor reports and the bridge resumes from the
committed cursor, so nothing is duplicated or relaunched (TR-036, TR-044).
"""

import asyncio
import logging
import threading
from contextlib import suppress
from typing import Any

from robin import notifications
from robin.events.supervisor_source import SupervisorRecord
from robin.ids import utc_now
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
PRESENCE_S = 45.0  # the dashboard repeats its presence every 20 s while visible


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
        self._notifier: asyncio.Task[None] | None = None
        # session id -> loop time until which a visible dashboard is showing it (FR-033: stay quiet there)
        self.viewing: dict[str, float] = {}
        self._watch_stop = threading.Event()
        self.index_error: dict[str, Any] | None = None  # a retryable indexing failure, reported once
        self._client: SupervisorClient | None = None

    async def start(self) -> None:
        await self.manager.recover()
        self._task = asyncio.create_task(self._link())
        self._indexer = asyncio.create_task(self._index())
        self._watcher = asyncio.create_task(self._watch())
        self._notifier = asyncio.create_task(self._notify_user())

    async def stop(self) -> None:
        self._watch_stop.set()
        for task in (self._task, self._indexer, self._watcher, self._notifier):
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
                self.index_error = None
            except Exception as exc:
                code = type(exc).__name__
                if self.index_error is None or self.index_error["code"] != code:  # once per kind of failure
                    log.warning("search indexing failed; retrying", extra={"stage": "index", "code": code})
                    self.index_error = {"code": code, "since": utc_now().isoformat(), "retries": 0}
                self.index_error["retries"] += 1
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

    def viewed(self, session_id: str | None, for_s: float = PRESENCE_S) -> None:
        """A dashboard showing this session is visible (or, with None, none is)."""
        now = asyncio.get_running_loop().time()
        if session_id is None:  # the dashboard was hidden or closed
            self.viewing = {}
            return
        self.viewing = {sid: t for sid, t in self.viewing.items() if t > now}
        self.viewing[session_id] = now + for_s

    async def _notify_user(self) -> None:
        """Opt-in notifications (FR-033), from committed events past a durable cursor."""
        seen = -1
        while True:
            seen = await self.wait_change(seen, 5.0)
            try:
                if not (await asyncio.to_thread(notifications.settings, self.store))["enabled"]:
                    continue
                notes, cursor = await asyncio.to_thread(notifications.pending, self.store)
                # the cursor moves first: a crash can drop a notification, never repeat one
                await asyncio.to_thread(notifications.advance, self.store, cursor)
                now = asyncio.get_running_loop().time()
                quiet = {sid for sid, t in self.viewing.items() if t > now}
                for note in notifications.condense([n for n in notes if n.session_id not in quiet]):
                    await asyncio.to_thread(notifications.send, note)
            except Exception:
                log.exception("notifications failed; retrying")
                await asyncio.sleep(5)

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
