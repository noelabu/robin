"""Watching native session history (FR-041, TR-081).

Off until the user turns it on (`robin watch on`): it means Robin keeps reading Claude Code, Codex and Pi
history as it grows. When on, a cycle stats every transcript under each runtime's history root and ingests
only the files that are new or changed, through the same crash-safe tailer as `robin scan`. Cycles run when
macOS reports a change (kqueue on the history folders and on the most recently active transcripts) and at
least every POLL_S seconds as a fallback, so an append to a quiet file is never missed for long.
"""

import logging
import os
import select
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select as sql_select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.adapters.history import HISTORY
from robin.discovery import RuntimeScan, _ingest_session
from robin.store import Store
from robin.store.models import Meta
from robin.workspace import tasks as ws

log = logging.getLogger(__name__)

SETTING = "watch_history"
POLL_S = 30.0
HOT_FILES = 64  # transcripts watched individually for appends
MAX_DIRS = 512  # history folders watched for new files


def enabled(store: Store) -> bool:
    with store.reader.connect() as conn:
        return conn.execute(sql_select(Meta.value).where(Meta.key == SETTING)).scalar() == "on"


def set_enabled(store: Store, on: bool) -> None:
    value = "on" if on else "off"
    store.writer.call(
        lambda c: c.execute(
            sqlite_insert(Meta)
            .values(key=SETTING, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
    )


@dataclass
class Cycle:
    changed_files: int = 0
    reports: list[RuntimeScan] = field(default_factory=list)


class HistoryWatcher:
    def __init__(self, store: Store, roots: dict[str, Path] | None = None):
        self.store = store
        self.roots = roots or {name: a.default_root() for name, a in HISTORY.items()}
        self.seen: dict[Path, tuple[int, int]] = {}  # path -> (size, mtime_ns) last ingested

    def _changed(self) -> dict[str, set[Path]]:
        out: dict[str, set[Path]] = {}
        for name, root in self.roots.items():
            if not root.is_dir():
                continue
            try:
                files = HISTORY[name].files(root)
            except OSError:
                continue
            for path in files:
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = (st.st_size, st.st_mtime_ns)
                if self.seen.get(path) != key:
                    out.setdefault(name, set()).add(path)
        return out

    def cycle(self) -> Cycle:
        """Ingest what changed since the last cycle. Errors are reported per file and never stop the rest."""
        result = Cycle()
        changed = self._changed()
        projects = ws.project_paths(self.store) if changed else []
        for name, paths in changed.items():
            adapter = HISTORY[name]
            report = RuntimeScan(name, str(self.roots[name]))
            result.reports.append(report)
            result.changed_files += len(paths)
            failed: set[Path] = set()
            for main in sorted({adapter.main(p) for p in paths}):
                if not main.is_file():
                    continue
                if not adapter.ready(main):  # read again next cycle, once it can be identified
                    failed.add(main)
                    continue
                report.found += 1
                try:
                    _ingest_session(self.store, adapter.describe(main), adapter.mapper, projects, report)
                except Exception as exc:
                    report.errors.append(f"{main.name}: {type(exc).__name__}")
                    failed.add(main)
            for p in paths:  # remember what was read, so the next cycle only sees new growth
                if adapter.main(p) in failed:
                    continue  # not read: try it again next cycle
                try:
                    st = p.stat()
                    self.seen[p] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    self.seen.pop(p, None)
        return result

    def hot_files(self) -> list[Path]:
        return [p for p, _ in sorted(self.seen.items(), key=lambda kv: -kv[1][1])[:HOT_FILES]]

    def watched_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for root in self.roots.values():
            if not root.is_dir():
                continue
            for d, subdirs, _ in os.walk(root):
                dirs.append(Path(d))
                if len(dirs) >= MAX_DIRS:
                    return dirs
                subdirs.sort()
        return dirs


class Waker:
    """Blocks until macOS reports a change to a watched folder or file, or the timeout passes (kqueue)."""

    def __init__(self) -> None:
        self.kq = select.kqueue() if hasattr(select, "kqueue") else None
        self.fds: list[int] = []

    def watch(self, paths: list[Path]) -> None:
        self.close_fds()
        if self.kq is None:
            return
        events = []
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_EVTONLY", 0))
            except OSError:
                continue
            self.fds.append(fd)
            events.append(
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE,
                )
            )
        if events:
            self.kq.control(events, 0, 0)

    def wait(self, timeout: float, stop: threading.Event | None = None) -> bool:
        """True when a change was reported, False on timeout (the polling fallback) or when stopped.
        Waits in one-second slices so a stop is noticed quickly."""
        stop = stop or threading.Event()
        remaining = timeout
        while remaining > 0 and not stop.is_set():
            step = min(1.0, remaining)
            if self.kq is None or not self.fds:
                stop.wait(step)
            elif self.kq.control(None, 16, step):
                return True
            remaining -= step
        return False

    def close_fds(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []

    def close(self) -> None:
        self.close_fds()
        if self.kq is not None:
            self.kq.close()


def run_once(
    watcher: HistoryWatcher, waker: Waker, timeout: float = POLL_S, stop: threading.Event | None = None
) -> dict[str, Any]:
    """One watch step: ingest changes, re-arm notifications, wait for the next change or the poll."""
    c = watcher.cycle()
    waker.watch(watcher.watched_dirs() + watcher.hot_files())
    woke = waker.wait(timeout, stop)
    errors = [e for r in c.reports for e in r.errors]
    if errors:
        log.warning("history watch: %d file(s) couldn't be read", len(errors))
    return {"changed_files": c.changed_files, "woke": woke, "errors": len(errors)}
