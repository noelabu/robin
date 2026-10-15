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


def claude_main(path: Path) -> Path:
    """A subagent transcript (<project>/<session>/subagents/x.jsonl) belongs to <project>/<session>.jsonl."""
    if path.parent.name == "subagents":
        return path.parent.parent.parent / f"{path.parent.parent.name}.jsonl"
    return path


def claude_describe(path: Path) -> NativeSession:
    recs = first_records(path)
    meta = next((r for r in recs if r.get("sessionId")), {})
    native = meta.get("sessionId") or path.stem
    subagents = tuple(sorted((path.parent / path.stem / "subagents").glob("*.jsonl")))
    return NativeSession("claude", str(native), path, meta.get("cwd"), meta.get("version"), subagents)


def claude_discover(root: Path) -> Iterator[NativeSession]:
    for path in claude_files(root):
        yield claude_describe(path)


def claude_mapper(rec: Any, ctx: MapperContext) -> MappedRecord | None:
    if not isinstance(rec, dict):
        return MappedRecord()
    kind = rec.get("type")
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


def codex_describe(path: Path) -> NativeSession:
    meta = next(
        (r.get("payload") or {} for r in first_records(path, 5) if r.get("type") == "session_meta"), {}
    )
    native = meta.get("id") or path.stem.split("-", 6)[-1]
    return NativeSession("codex", str(native), path, meta.get("cwd"), meta.get("cli_version"))


def codex_discover(root: Path) -> Iterator[NativeSession]:
    for path in codex_files(root):
        yield codex_describe(path)


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


def codex_mapper(rec: Any, ctx: MapperContext) -> MappedRecord | None:
    if not isinstance(rec, dict):
        return MappedRecord()
    kind = rec.get("type")
    raw = rec.get("payload")
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if kind == "session_meta":
        return _m("session.started", rec)
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
        lambda root: claude_files(root) + sorted(root.glob("*/*/subagents/*.jsonl")),
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
