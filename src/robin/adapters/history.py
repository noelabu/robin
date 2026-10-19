"""Discovering native sessions and mapping their transcript records (FR-010, FR-011).

Discovery only reads: it never writes to a runtime's store. Each runtime contributes a store root, a
discoverer that reads just enough of each file to identify its session, and a record mapper for the
JSONL tailer. Unknown record types map to unknown.raw and are kept, never dropped (FR-011).
"""

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robin.events.envelope import EvidenceKind
from robin.events.tailer import MappedRecord, MapperContext, RecordMapper

HEADER_BYTES = 256 << 10  # read at most this much of a file to identify it


@dataclass(frozen=True)
class NativeSession:
    runtime: str
    native_id: str
    path: Path  # the session's main transcript
    cwd: str | None
    version: str | None
    extra_files: tuple[Path, ...] = field(default_factory=tuple)  # e.g. Claude subagent transcripts


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, UTC)
    if isinstance(value, str):
        try:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    return None


def first_records(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """The first complete JSON records of a file, read within HEADER_BYTES."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
    except OSError:
        return []  # identify by filename; the read failure is reported when the file is ingested
    out = []
    for line in head.split(b"\n")[:-1][:limit]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _m(event_type: str, rec: dict[str, Any], native_id_key: str | None = None) -> MappedRecord:
    return MappedRecord(
        event_type=event_type,
        evidence_kind=EvidenceKind.REPORTED,
        source_time=parse_time(rec.get("timestamp")),
        native_event_id=str(rec[native_id_key]) if native_id_key and rec.get(native_id_key) else None,
    )


# -- Claude Code ------------------------------------------------------------------


def claude_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/*.jsonl"))


def claude_workflow_file(path: Path) -> bool:
    """A workflow run's agent transcript or journal: <project>/<session>/subagents/workflows/<run>/x.jsonl."""
    return (
        len(path.parents) > 4 and path.parents[1].name == "workflows" and path.parents[2].name == "subagents"
    )


def claude_main(path: Path) -> Path:
    """A subagent transcript (<project>/<session>/subagents/x.jsonl) belongs to <project>/<session>.jsonl."""
    if path.parent.name == "subagents":
        return path.parent.parent.parent / f"{path.parent.parent.name}.jsonl"
    if claude_workflow_file(path):
        return path.parents[4] / f"{path.parents[3].name}.jsonl"
    return path


def claude_describe(path: Path) -> NativeSession:
    # Metadata records (last-prompt, mode, permission-mode) lead the file and carry the session ID but not
    # the folder or version, so each field comes from the first record that has it.
    recs = first_records(path, limit=200)
    native = next((r["sessionId"] for r in recs if r.get("sessionId")), None) or path.stem
    cwd = next((r["cwd"] for r in recs if r.get("cwd")), None)
    version = next((r["version"] for r in recs if r.get("version")), None)
    sub = path.parent / path.stem / "subagents"
    subagents = tuple(sorted(sub.glob("*.jsonl")) + sorted(sub.glob("workflows/*/*.jsonl")))
    return NativeSession("claude", str(native), path, cwd, version, subagents)


def claude_discover(root: Path) -> Iterator[NativeSession]:
    for path in claude_files(root):
        yield claude_describe(path)


NOTIFICATION = "<task-notification>"


def claude_mapper(rec: Any, ctx: MapperContext) -> MappedRecord | None:
    if not isinstance(rec, dict):
        return MappedRecord()
    kind = rec.get("type")
    if ctx.source_id.endswith("/journal.jsonl") and claude_workflow_file(Path(ctx.source_id[5:])):
        # A workflow run's journal: which agent started, in which phase, and how it ended. No timestamps.
        return _m("tool.progress", rec) if kind in ("started", "result", "failed") else MappedRecord()
    # Background work finishing; sometimes these queue records are the only copy of the notification.
    attachment = rec.get("attachment")
    if kind == "attachment" and isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        prompt = attachment.get("prompt")
        if isinstance(prompt, str) and prompt.startswith(NOTIFICATION):
            return _m("tool.finished", rec)
    if kind == "queue-operation" and rec.get("operation") == "enqueue":
        content = rec.get("content")
        if isinstance(content, str) and content.startswith(NOTIFICATION):
            return _m("tool.finished", rec)
    content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else None
    blocks = content if isinstance(content, list) else []
    if kind == "user":
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
            return _m("tool.result", rec, "uuid")
        return _m("message.user", rec, "uuid")
    if kind == "assistant":
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return _m("tool.started", rec, "uuid")
        return _m("message.completed", rec, "uuid")
    if kind in ("launched", "started"):  # subagent transcripts
        return _m("session.started", rec)
    if kind == "result":
        return _m("session.turn_completed", rec)
    return MappedRecord(source_time=parse_time(rec.get("timestamp")))


# -- Codex ------------------------------------------------------------------------


def codex_files(root: Path) -> list[Path]:
    return sorted(root.rglob("rollout-*.jsonl"))


def codex_thread(path: Path) -> str:
    return path.stem.split("-", 6)[-1]


_HEADS: dict[str, dict[str, Any]] = {}


def codex_head(path: Path) -> dict[str, Any] | None:
    """A rollout's own thread, the thread that spawned it and how, from its first line (session_meta).
    None while that line is still being written; a complete one is final, so it's read once."""
    key = str(path)
    if key in _HEADS:
        return _HEADS[key]
    try:
        with open(path, "rb") as f:
            line = f.readline(HEADER_BYTES)
    except OSError:
        return None
    if not line.endswith(b"\n") and len(line) < HEADER_BYTES:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        rec = None
    meta = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "session_meta" else None
    meta = meta if isinstance(meta, dict) else {}
    source = meta.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
    head = {
        "id": str(meta.get("id") or codex_thread(path)),
        "parent": str(parent) if parent else None,
        "agent_path": spawn.get("agent_path") or meta.get("agent_path"),
        "agent_role": spawn.get("agent_role") or meta.get("agent_role"),
        "agent_nickname": spawn.get("agent_nickname") or meta.get("agent_nickname"),
    }
    _HEADS[key] = head
    return head


def codex_ready(path: Path) -> bool:
    return codex_head(path) is not None


def _codex_root(path: Path) -> Path:
    """The sessions folder: rollouts live in <root>/YYYY/MM/DD/."""
    day = path.parent
    if all(p.name.isdigit() for p in (day, day.parent, day.parent.parent)):
        return day.parents[2]
    return day


def _codex_groups(root: Path) -> dict[Path, list[Path]]:
    """Each top rollout (no parent among the files) -> the subagent rollouts under it, at any depth."""
    files = codex_files(root)
    by_thread = {codex_thread(p): p for p in files}

    def top(path: Path) -> Path:
        seen = {path}
        while (head := codex_head(path)) and head["parent"] in by_thread:
            parent = by_thread[head["parent"]]
            if parent in seen:
                break
            path = parent
            seen.add(path)
        return path

    groups: dict[Path, list[Path]] = {}
    for path in files:
        main = top(path)
        members = groups.setdefault(main, [])
        if path != main:
            members.append(path)
    return groups


_MAINS: dict[str, Path] = {}  # subagent rollout -> its root thread's rollout, once that root is found


def codex_main(path: Path) -> Path:
    """A subagent's rollout belongs to the topmost rollout above it that is present."""
    if (head := codex_head(path)) is None or not head["parent"]:
        return path
    if str(path) not in _MAINS:
        for main, kids in _codex_groups(_codex_root(path)).items():
            if (top := codex_head(main)) is not None and not top["parent"]:
                _MAINS.update((str(k), main) for k in kids)
    return _MAINS.get(str(path), path)


def codex_describe(path: Path, extra: tuple[Path, ...] | None = None) -> NativeSession:
    meta = next(
        (r.get("payload") or {} for r in first_records(path, 5) if r.get("type") == "session_meta"), {}
    )
    native = meta.get("id") or codex_thread(path)
    if extra is None:
        extra = tuple(sorted(_codex_groups(_codex_root(path)).get(path, [])))
    return NativeSession("codex", str(native), path, meta.get("cwd"), meta.get("cli_version"), extra)


def codex_discover(root: Path) -> Iterator[NativeSession]:
    for main, kids in _codex_groups(root).items():
        if codex_ready(main):  # not identifiable yet: the next scan reads it
            yield codex_describe(main, tuple(sorted(kids)))


CODEX_EVENTS = {
    "user_message": "message.user",
    "agent_message": "message.completed",
    "task_started": "session.turn_started",
    "task_complete": "session.turn_completed",
    "turn_aborted": "session.turn_completed",
    "exec_command_begin": "tool.started",
    "exec_command_end": "tool.result",
    "patch_apply_end": "file.change",
    "error": "session.failed",
}
CODEX_ITEMS = {
    "function_call": "tool.started",
    "function_call_output": "tool.result",
    "custom_tool_call": "tool.started",
    "custom_tool_call_output": "tool.result",
}
CODEX_COMPLETED = {  # event_msg item_completed, by item type
    "UserMessage": "message.user",
    "AgentMessage": "message.completed",
    "CommandExecution": "tool.started",
    "SubAgentActivity": "tool.started",
    "FileChange": "file.change",
}


def codex_mapper(rec: Any, ctx: MapperContext) -> MappedRecord | None:
    if not isinstance(rec, dict):
        return MappedRecord()
    kind = rec.get("type")
    raw = rec.get("payload")
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if kind == "session_meta":
        return _m("session.started", rec)
    if (
        kind == "event_msg"
        and payload.get("type") == "item_completed"
        and isinstance(payload.get("item"), dict)
    ):
        item = payload["item"]
        if item.get("type") in CODEX_COMPLETED:
            return MappedRecord(
                event_type=CODEX_COMPLETED[item["type"]],
                evidence_kind=EvidenceKind.REPORTED,
                source_time=parse_time(rec.get("timestamp")),
                native_event_id=str(item["id"]) if item.get("id") else None,
            )
    if kind == "event_msg" and payload.get("type") in CODEX_EVENTS:
        return _m(CODEX_EVENTS[payload["type"]], rec)
    if kind == "response_item":
        if payload.get("type") == "message":
            return _m("message.user" if payload.get("role") == "user" else "message.completed", rec)
        if payload.get("type") in CODEX_ITEMS:
            return MappedRecord(
                event_type=CODEX_ITEMS[payload["type"]],
                evidence_kind=EvidenceKind.REPORTED,
                source_time=parse_time(rec.get("timestamp")),
                native_event_id=payload.get("call_id"),
            )
    return MappedRecord(source_time=parse_time(rec.get("timestamp")))


# -- Pi ---------------------------------------------------------------------------


def pi_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/*.jsonl"))


def pi_describe(path: Path) -> NativeSession:
    header = next((r for r in first_records(path, 2) if r.get("type") == "session"), {})
    native = header.get("id") or path.stem.split("_", 1)[-1]
    version = str(header["version"]) if "version" in header else None
    return NativeSession("pi", str(native), path, header.get("cwd"), version)


def pi_discover(root: Path) -> Iterator[NativeSession]:
    for path in pi_files(root):
        yield pi_describe(path)


def pi_mapper(rec: Any, ctx: MapperContext) -> MappedRecord | None:
    if not isinstance(rec, dict):
        return MappedRecord()
    kind = rec.get("type")
    if kind == "session":
        return _m("session.started", rec, "id")
    if kind == "message":
        role = (rec.get("message") or {}).get("role")
        event_type = {
            "user": "message.user",
            "assistant": "message.completed",
            "toolResult": "tool.result",
        }.get(str(role), "unknown.raw")
        return _m(event_type, rec, "id")
    return MappedRecord(source_time=parse_time(rec.get("timestamp")))


@dataclass(frozen=True)
class HistoryAdapter:
    runtime: str
    home_root: Callable[[], Path]
    discover: Callable[[Path], Iterator[NativeSession]]
    mapper: RecordMapper
    files: Callable[[Path], list[Path]]  # every transcript a watcher should follow, subagents included
    describe: Callable[[Path], NativeSession]  # one main transcript
    main: Callable[[Path], Path] = lambda p: p  # the main transcript a file belongs to
    ready: Callable[[Path], bool] = lambda p: True  # False while a main transcript can't be identified yet

    def default_root(self) -> Path:
        """The runtime's own store; ROBIN_HISTORY_<RUNTIME> points elsewhere (tests, unusual installs)."""
        override = os.environ.get(f"ROBIN_HISTORY_{self.runtime.upper()}")
        return Path(override) if override else self.home_root()


HISTORY: dict[str, HistoryAdapter] = {
    "claude": HistoryAdapter(
        "claude",
        lambda: Path.home() / ".claude" / "projects",
        claude_discover,
        claude_mapper,
        lambda root: (
            claude_files(root)
            + sorted(root.glob("*/*/subagents/*.jsonl"))
            + sorted(root.glob("*/*/subagents/workflows/*/*.jsonl"))
        ),
        claude_describe,
        claude_main,
    ),
    "codex": HistoryAdapter(
        "codex",
        lambda: Path.home() / ".codex" / "sessions",
        codex_discover,
        codex_mapper,
        codex_files,
        codex_describe,
        codex_main,
        codex_ready,
    ),
    "pi": HistoryAdapter(
        "pi",
        lambda: Path.home() / ".pi" / "agent" / "sessions",
        pi_discover,
        pi_mapper,
        pi_files,
        pi_describe,
    ),
}
