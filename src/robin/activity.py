"""What each session is doing, for the Map: its title, last tool and target, todo list and changed files.

A projector folds committed events into `session_activity` behind a watermark, off the request path, the
way search indexing does. Claude subagent transcripts get their own lane. It understands Claude Code and
Codex transcripts, the host events of sessions Robin runs, and hook events; Pi gets a title only. Every
stored string is secret-scanned and truncated first (TR-070), and the table is rebuilt from the events when
the extractor or the redaction rules change.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.events.ingest import read_payload_json
from robin.redact.scanner import RuleSet, for_data_dir
from robin.store import Store
from robin.store.models import Event, Meta, SessionActivity
from robin.store.models import Session as SessionRow

VERSION = "3"  # bump when extraction changes: the table is rebuilt from the events
CURSOR_KEY = "activity_cursor"
RULES_KEY = "activity_rules"
BATCH = 500
TEXT_LIMIT = 120
TODO_LIMIT = 140
MAX_TODOS = 50
MAX_FILES = 500
RESULT_LIMIT = 8192  # tool results are read only to learn a new task's id
EVENT_TYPES = (
    "message.user",
    "message.completed",
    "tool.started",
    "tool.result",
    "file.change",
    "session.started",
    "session.turn_started",
    "session.turn_completed",
    "session.plan_updated",
)
PAYLOAD_TYPES = {
    "message.user",
    "tool.started",
    "file.change",
    "session.turn_started",
    "session.plan_updated",
}
EDIT_TOOLS = {"Edit": "edit", "MultiEdit": "edit", "Write": "write", "NotebookEdit": "edit"}
STATUSES = {"pending": "pending", "in_progress": "in_progress", "inProgress": "in_progress",
            "completed": "completed", "done": "completed"}  # fmt: skip
WRAPPED = re.compile(r"<([A-Za-z][\w-]*)\b[^>]*>.*?</\1>", re.S)  # <system-reminder>…</system-reminder>
PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(</pasted_content>|$)", re.S)  # may be cut off unclosed
PATCH_LINE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


@dataclass
class Lane:
    title: str | None = None
    label: str | None = None
    tool: str | None = None
    target: str | None = None
    action_at: datetime | None = None
    turn_started_at: datetime | None = None
    last_activity_at: datetime | None = None
    todos: list[dict[str, str]] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    pending: dict[str, str] = field(default_factory=dict)  # TaskCreate tool_use id -> local task id


@dataclass
class Ctx:
    rules: RuleSet
    cwd: str | None
    at: datetime


# -- summaries -----------------------------------------------------------------------------------------------


def summarize(rules: RuleSet, value: Any, limit: int = TEXT_LIMIT) -> str:
    """Redact before truncating, so a secret cut in half can't slip past the patterns."""
    text = " ".join(rules.redact(str(value)[:4096])[0].split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rel(ctx: Ctx, path: str) -> str:
    if ctx.cwd and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, ctx.cwd)
        except ValueError:
            rel = path
        if not rel.startswith(".."):
            path = rel
    return summarize(ctx.rules, path, 300)


def _target(ctx: Ctx, tool: str, inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(inp.get(key), str):
            return _rel(ctx, inp[key])
    command = inp.get("command", inp.get("cmd"))
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    if isinstance(command, str) and command.strip():
        return summarize(ctx.rules, command.strip().splitlines()[0])
    if isinstance(inp.get("url"), str):
        return summarize(ctx.rules, re.sub(r"^\w+://([^/]+).*$", r"\1", inp["url"]))
    for key in ("pattern", "description", "query", "subject", "prompt"):
        if isinstance(inp.get(key), str):
            return summarize(ctx.rules, inp[key])
    return None


# -- folding ---------------------------------------------------------------------------------------------


def _touch(lane: Lane, ctx: Ctx) -> None:
    if lane.last_activity_at is None or ctx.at >= lane.last_activity_at:
        lane.last_activity_at = ctx.at


def _title(lane: Lane, ctx: Ctx, text: Any) -> None:
    if not isinstance(text, str):
        return
    text = PASTED.sub(" [pasted] ", WRAPPED.sub("", text)).strip()
    if not text or text.startswith("<"):  # command wrappers, caveats, injected context
        return
    lane.turn_started_at = ctx.at
    if lane.title is None:
        lane.title = summarize(ctx.rules, text)


def _files(lane: Lane, ctx: Ctx, paths: list[tuple[str, str]]) -> None:
    for path, op in paths:
        if not path:
            continue
        rel = _rel(ctx, path)
        lane.files = [f for f in lane.files if f["path"] != rel]
        lane.files.insert(0, {"path": rel, "op": op, "at": ctx.at.isoformat()})
    del lane.files[MAX_FILES:]


def _todo(ctx: Ctx, tid: str, text: Any, status: Any) -> dict[str, str]:
    return {
        "id": tid,
        "text": summarize(ctx.rules, text or "", TODO_LIMIT),
        "status": STATUSES.get(str(status), "pending"),
    }


def _patch_paths(patch: str) -> list[tuple[str, str]]:
    out = []
    for m in PATCH_LINE.finditer(patch):
        if m.group(3):
            out.append((m.group(3).strip(), "move"))
        else:
            out.append(
                (m.group(2).strip(), {"Add": "write", "Update": "edit", "Delete": "delete"}[m.group(1)])
            )
    return out


def _tool(lane: Lane, ctx: Ctx, name: Any, inp: Any, call_id: str | None = None) -> None:
    if not isinstance(name, str) or not name:
        return
    if isinstance(inp, str) and name in ("update_plan", "exec_command", "shell"):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {}
    lane.tool, lane.target, lane.action_at = summarize(ctx.rules, name, 60), _target(ctx, name, inp), ctx.at
    if not isinstance(inp, dict):
        if name == "apply_patch" and isinstance(inp, str):
            paths = _patch_paths(inp)
            _files(lane, ctx, paths)
            lane.target = _rel(ctx, paths[0][0]) if paths else None
        return
    edited = inp.get("file_path", inp.get("notebook_path"))
    if name in EDIT_TOOLS and isinstance(edited, str):
        _files(lane, ctx, [(edited, EDIT_TOOLS[name])])
    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
        lane.todos = [
            _todo(ctx, str(i), t.get("content"), t.get("status"))
            for i, t in enumerate(inp["todos"][:MAX_TODOS], 1)
            if isinstance(t, dict)
        ]
    elif name == "update_plan" and isinstance(inp.get("plan"), list):
        _plan(lane, ctx, inp["plan"])
    elif name == "TaskCreate" and len(lane.todos) < MAX_TODOS:
        tid = str(max((int(t["id"]) for t in lane.todos if t["id"].isdigit()), default=0) + 1)
        lane.todos.append(_todo(ctx, tid, inp.get("subject"), "pending"))
        if call_id:
            lane.pending[call_id] = tid
    elif name == "TaskUpdate" and inp.get("taskId") is not None:
        tid = str(inp["taskId"])
        if inp.get("status") == "deleted":
            lane.todos = [t for t in lane.todos if t["id"] != tid]
            return
        for t in lane.todos:
            if t["id"] == tid:
                if inp.get("status"):
                    t["status"] = STATUSES.get(str(inp["status"]), t["status"])
                if inp.get("subject"):
                    t["text"] = summarize(ctx.rules, inp["subject"], TODO_LIMIT)


def _plan(lane: Lane, ctx: Ctx, plan: list[Any]) -> None:
    lane.todos = [
        _todo(ctx, str(i), t.get("step"), t.get("status"))
        for i, t in enumerate(plan[:MAX_TODOS], 1)
        if isinstance(t, dict)
    ]


def _task_result(lane: Lane, rec: dict[str, Any]) -> None:
    """A TaskCreate result names the task's real id; renumber the todo created for it."""
    blocks = (rec.get("message") or {}).get("content")
    for b in blocks if isinstance(blocks, list) else []:
        if not (
            isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in lane.pending
        ):
            continue
        local = lane.pending.pop(b["tool_use_id"])
        task = (
            (rec.get("toolUseResult") or {}).get("task")
            if isinstance(rec.get("toolUseResult"), dict)
            else None
        )
        real = str(task["id"]) if isinstance(task, dict) and task.get("id") is not None else None
        if real is None:
            m = re.search(r"Task #(\w+)", json.dumps(b.get("content")))
            real = m.group(1) if m else None
        if real and real != local and not any(t["id"] == real for t in lane.todos):
            for t in lane.todos:
                if t["id"] == local:
                    t["id"] = real


def fold(lane: Lane, ctx: Ctx, event_type: str, payload: Any) -> None:
    """Apply one event's payload to a lane. Shapes it doesn't know change nothing and never raise (NFR-06)."""
    _touch(lane, ctx)
    if not isinstance(payload, dict) or payload.get("_robin_pruned"):
        return
    kind = payload.get("type")
    if "hook" in payload:  # hook events: signals only
        if payload.get("hook") in ("PreToolUse", "tool_call"):
            _tool(lane, ctx, payload.get("tool_name"), payload.get("tool_input"))
        elif payload.get("hook") == "UserPromptSubmit":
            _title(lane, ctx, payload.get("prompt"))
        return
    if kind in ("assistant", "user") and isinstance(payload.get("message"), dict):  # Claude transcript
        content = payload["message"].get("content")
        if kind == "user":
            if event_type == "tool.result":
                _task_result(lane, payload)
            elif not payload.get("isMeta"):
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )
                _title(lane, ctx, content)
            return
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                _tool(lane, ctx, b.get("name"), b.get("input"), b.get("id"))
        return
    if kind in ("event_msg", "response_item"):  # Codex rollout
        raw = payload.get("payload")
        p: dict[str, Any] = raw if isinstance(raw, dict) else {}
        ptype = p.get("type")
        if kind == "event_msg":
            if ptype == "user_message":
                _title(lane, ctx, p.get("message"))
            elif ptype == "task_started":
                lane.turn_started_at = ctx.at
            elif ptype == "patch_apply_end" and isinstance(p.get("changes"), dict):
                _files(lane, ctx, [(str(k), "edit") for k in p["changes"]])
            elif ptype == "exec_command_begin":
                _tool(lane, ctx, "shell", {"command": p.get("command")})
        elif ptype == "function_call":
            _tool(lane, ctx, p.get("name"), p.get("arguments"))
        elif ptype == "custom_tool_call":
            _tool(lane, ctx, p.get("name"), p.get("input"))
        return
    if kind == "message" and isinstance(payload.get("message"), dict):  # Pi transcript
        msg = payload["message"]
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            _title(lane, ctx, content)
        return
    # Host events from sessions Robin runs.
    if kind == "user_message":
        _title(lane, ctx, payload.get("text"))
    elif kind == "turn_started":
        lane.turn_started_at = ctx.at
    elif kind == "tool_started":
        _tool(lane, ctx, payload.get("tool"), payload.get("input"))
    elif kind == "file_changed" and isinstance(payload.get("paths"), list):
        _files(lane, ctx, [(str(p), "edit") for p in payload["paths"] if p])
    elif kind == "plan_updated" and isinstance(payload.get("plan"), list):
        _plan(lane, ctx, payload["plan"])


# -- lanes ---------------------------------------------------------------------------------------------------


def lane_of(source_id: str) -> str:
    """Claude subagents live in <session>/subagents/agent-*.jsonl and are tailed into their parent session."""
    if source_id.startswith("file:"):
        path = Path(source_id[5:])
        if path.parent.name == "subagents":
            return path.stem
    return ""


def _subagent_meta(rules: RuleSet, source_id: str) -> tuple[str | None, str | None]:
    meta = Path(source_id[5:]).with_suffix(".meta.json")
    try:
        raw = json.loads(meta.read_text()) if meta.stat().st_size < 65536 else {}
    except (OSError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    label = summarize(rules, raw["agentType"], 40) if isinstance(raw.get("agentType"), str) else None
    title = summarize(rules, raw["description"]) if isinstance(raw.get("description"), str) else None
    return label, title


def _load(row: Any) -> Lane:
    scratch = json.loads(row.scratch or "{}")
    return Lane(
        title=row.title,
        label=row.label,
        tool=row.tool,
        target=row.target,
        action_at=row.action_at,
        turn_started_at=row.turn_started_at,
        last_activity_at=row.last_activity_at,
        todos=json.loads(row.todos or "[]"),
        files=json.loads(row.files or "[]"),
        pending=scratch.get("pending", {}),
    )


def _row(session_id: str, lane_id: str, lane: Lane) -> dict[str, Any]:
    return dict(
        session_id=session_id,
        lane=lane_id,
        title=lane.title,
        label=lane.label,
        tool=lane.tool,
        target=lane.target,
        action_at=lane.action_at,
        turn_started_at=lane.turn_started_at,
        last_activity_at=lane.last_activity_at,
        todos=json.dumps(lane.todos),
        todo_done=sum(t["status"] == "completed" for t in lane.todos),
        todo_total=len(lane.todos),
        files=json.dumps(lane.files),
        files_count=len(lane.files),
        scratch=json.dumps({"pending": lane.pending}),
    )


# -- projector -----------------------------------------------------------------------------------------------


def _fingerprint(store: Store) -> str:
    custom = store.data_dir / "redact.toml"
    mtime = custom.stat().st_mtime if custom.exists() else 0.0
    return f"{VERSION}|{for_data_dir(store.data_dir).version}|{mtime}"


def _meta(conn: Connection, key: str) -> str | None:
    return conn.execute(select(Meta.value).where(Meta.key == key)).scalar()


def _set_meta(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        sqlite_insert(Meta)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


class Stale(Exception):
    """Another pass moved the watermark first; this batch is dropped and re-read."""


def project_batch(store: Store, batch: int = BATCH) -> int:
    """Fold the next batch of relevant events into session_activity. Returns how many it folded."""
    fingerprint = _fingerprint(store)
    with store.reader.connect() as conn:
        current = _meta(conn, RULES_KEY)
    if current != fingerprint:

        def reset(c: Connection) -> None:
            c.execute(delete(SessionActivity))
            _set_meta(c, CURSOR_KEY, "0")
            _set_meta(c, RULES_KEY, fingerprint)

        store.writer.call(reset)
    with store.reader.connect() as conn:
        after = int(_meta(conn, CURSOR_KEY) or 0)
        head = int(conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one())
        if head <= after:
            return 0
        rows = conn.execute(
            select(
                Event.global_cursor,
                Event.session_id,
                Event.event_type,
                Event.source_id,
                Event.source_time,
                Event.ingested_at,
                Event.payload_size,
                SessionRow.execution_cwd,
            )
            .join(SessionRow, SessionRow.session_id == Event.session_id)
            .where(
                Event.global_cursor > after, Event.global_cursor <= head, Event.event_type.in_(EVENT_TYPES)
            )
            .order_by(Event.global_cursor)
            .limit(batch)
        ).all()
        keys = {(r.session_id, lane_of(r.source_id)) for r in rows}
        existing = {}
        for sid in {k[0] for k in keys}:
            for row in conn.execute(select(SessionActivity).where(SessionActivity.session_id == sid)).all():
                existing[(row.session_id, row.lane)] = _load(row)
    last = rows[-1].global_cursor if len(rows) == batch else head
    rules = for_data_dir(store.data_dir)
    lanes: dict[tuple[str, str], Lane] = {}
    for r in rows:
        key = (r.session_id, lane_of(r.source_id))
        lane = lanes.get(key) or existing.get(key)
        if lane is None:
            lane = Lane()
            if key[1]:
                lane.label, lane.title = _subagent_meta(rules, r.source_id)
        lanes[key] = lane
        ctx = Ctx(rules, r.execution_cwd, r.source_time or r.ingested_at)
        wanted = r.event_type in PAYLOAD_TYPES or (
            r.event_type == "tool.result" and lane.pending and r.payload_size < RESULT_LIMIT
        )
        payload = None
        if wanted:
            try:
                payload = read_payload_json(store, r.global_cursor)
            except (ValueError, OSError):
                payload = None
        fold(lane, ctx, r.event_type, payload)

    def work(conn: Connection) -> None:
        if int(_meta(conn, CURSOR_KEY) or 0) != after or _meta(conn, RULES_KEY) != fingerprint:
            raise Stale
        for (sid, lane_id), lane in lanes.items():
            values = _row(sid, lane_id, lane)
            conn.execute(
                sqlite_insert(SessionActivity)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["session_id", "lane"],
                    set_={k: v for k, v in values.items() if k not in ("session_id", "lane")},
                )
            )
        _set_meta(conn, CURSOR_KEY, str(last))

    try:
        store.writer.call(work)
    except Stale:
        return 0
    return len(rows) or 1  # moving past irrelevant events counts as progress


def catch_up(store: Store, max_batches: int = 20) -> int:
    """Fold whatever is pending, in bounded batches. Returns events folded."""
    n = 0
    for _ in range(max_batches):
        got = project_batch(store)
        n += got
        if got < BATCH:
            break
    return n


def lag(store: Store) -> int:
    with store.reader.connect() as conn:
        done = int(_meta(conn, CURSOR_KEY) or 0)
        head = int(conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one())
    return max(0, head - done)


# -- folders -------------------------------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def cluster_of(cwd: str | None) -> str:
    """The repository a folder belongs to (a worktree resolves to its main repository), or the folder."""
    if not cwd:
        return ""
    start = Path(cwd)
    for folder in (start, *start.parents):
        git = folder / ".git"
        try:
            if git.is_dir():
                return str(folder)
            if git.is_file():
                line = git.read_text(errors="replace").strip()
                if line.startswith("gitdir:"):
                    gitdir = Path(line[7:].strip())
                    if not gitdir.is_absolute():
                        gitdir = (folder / gitdir).resolve()
                    if gitdir.parent.name == "worktrees":
                        return str(gitdir.parents[2])
                return str(folder)
        except OSError:
            break
    return cwd


# -- the Map --------------------------------------------------------------------------------------------


DETAIL_RUNTIMES = ("claude", "codex")
RUNNING_WINDOW_S = 60.0  # a session with no reported state counts as working this long after activity
SUBAGENTS_SHOWN = 8


def _display_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + os.sep) else path


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _state(reported: str | None, last: datetime | None, now: datetime) -> tuple[str, str]:
    if reported and reported != "unknown":
        return reported, "reported"
    if last and (now - last).total_seconds() < RUNNING_WINDOW_S:
        return "running", "inferred"
    return "idle", "inferred"


def _card(s: Any, lane: Any, now: datetime, parent: str | None = None) -> dict[str, Any]:
    last = lane.last_activity_at if lane is not None else None  # not derived yet: unknown, not "just now"
    state, evidence = _state(None if parent else s.runtime_state, last, now)
    if parent and s.runtime_state in ("stopped", "failed"):
        state, evidence = "stopped", "inferred"
    total = lane.todo_total if lane is not None else 0
    return {
        "id": f"{s.session_id}:{lane.lane}" if parent else s.session_id,
        "session_id": s.session_id,
        "parent": parent,
        "runtime": s.runtime,
        "mode": s.mode,
        "label": lane.label if lane is not None else None,
        "title": (lane.title or lane.label) if lane is not None else None,
        "state": state,
        "state_evidence": evidence,
        "detail": s.runtime in DETAIL_RUNTIMES,
        "todos": {"done": lane.todo_done, "total": total} if total else None,
        "files": lane.files_count if lane is not None else 0,
        "last_file": (json.loads(lane.files)[0]["path"] if lane is not None and lane.files_count else None),
        "tool": lane.tool if lane is not None else None,
        "target": lane.target if lane is not None else None,
        "action_at": _iso(lane.action_at) if lane is not None else None,
        "turn_started_at": _iso(lane.turn_started_at) if lane is not None else None,
        "last_activity_at": _iso(last),
        "created_at": _iso(s.created_at),
        "subagents_hidden": 0,
    }


def _now_card(conn: Connection, now: datetime) -> dict[str, Any] | None:
    """The most recent action across all sessions and subagents."""
    recent = conn.execute(
        select(SessionActivity, SessionRow.runtime, SessionRow.execution_cwd)
        .join(SessionRow, SessionRow.session_id == SessionActivity.session_id)
        .where(SessionActivity.tool.is_not(None), SessionRow.archived.is_(False))
        .order_by(SessionActivity.last_activity_at.desc())
        .limit(20)
    ).all()
    if not recent:
        return None
    r = max(recent, key=lambda x: x.action_at or datetime.min.replace(tzinfo=now.tzinfo))
    return {
        "card_id": f"{r.session_id}:{r.lane}" if r.lane else r.session_id,
        "session_id": r.session_id,
        "runtime": r.runtime,
        "title": r.title or r.label,
        "tool": r.tool,
        "target": r.target,
        "action_at": _iso(r.action_at),
        "turn_started_at": _iso(r.turn_started_at),
        "cluster": cluster_of(r.execution_cwd),
    }


def _rows(conn: Connection, session_ids: list[str] | None) -> list[Any]:
    q = (
        select(
            SessionRow.session_id,
            SessionRow.runtime,
            SessionRow.mode,
            SessionRow.runtime_state,
            SessionRow.execution_cwd,
            SessionRow.created_at,
            SessionRow.updated_at,
            SessionActivity.lane,
            SessionActivity.title,
            SessionActivity.label,
            SessionActivity.tool,
            SessionActivity.target,
            SessionActivity.action_at,
            SessionActivity.turn_started_at,
            SessionActivity.last_activity_at,
            SessionActivity.todo_done,
            SessionActivity.todo_total,
            SessionActivity.files,
            SessionActivity.files_count,
        )
        .outerjoin(SessionActivity, SessionActivity.session_id == SessionRow.session_id)
        .where(SessionRow.archived.is_(False))
    )
    if session_ids is not None:
        q = q.where(SessionRow.session_id.in_(session_ids))
    return list(conn.execute(q).all())


def _cards(rows: list[Any], now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Session id -> [its card, then its most recent subagent cards]."""
    by_session: dict[str, tuple[Any, Any, list[Any]]] = {}
    for r in rows:
        s, main, subs = by_session.get(r.session_id, (r, None, []))
        if r.lane == "":
            main = r
        elif r.lane:
            subs.append(r)
        by_session[r.session_id] = (s, main, subs)
    out = {}
    for sid, (s, main, subs) in by_session.items():
        card = _card(s, main, now)
        if main is None and subs:  # only subagents seen so far: the parent still has activity
            card["last_activity_at"] = (
                _iso(max((x.last_activity_at for x in subs if x.last_activity_at), default=None))
                or card["last_activity_at"]
            )
        subs.sort(key=lambda x: x.last_activity_at or datetime.min.replace(tzinfo=now.tzinfo), reverse=True)
        card["subagents_hidden"] = max(0, len(subs) - SUBAGENTS_SHOWN)
        out[sid] = [card] + [_card(s, x, now, parent=sid) for x in subs[:SUBAGENTS_SHOWN]]
    return out


def map_snapshot(store: Store, per_cluster: int = 50, cluster: str | None = None) -> dict[str, Any]:
    now = datetime.now().astimezone()
    with store.reader.connect() as conn:
        rows = _rows(conn, None)
        now_card = _now_card(conn, now)
    groups: dict[str, list[list[dict[str, Any]]]] = {}
    cwd_of = {r.session_id: r.execution_cwd for r in rows}
    for sid, cards in _cards(rows, now).items():
        key = cluster_of(cwd_of[sid])
        for c in cards:
            c["cluster"] = key
        groups.setdefault(key, []).append(cards)
    clusters, out = [], []
    for key, sessions in groups.items():
        if cluster is not None and key != cluster:
            continue
        sessions.sort(key=lambda cs: cs[0]["created_at"] or "", reverse=True)
        shown = sessions if cluster is not None else sessions[:per_cluster]
        mains = [cs[0] for cs in sessions]
        clusters.append(
            {
                "key": key,
                "label": Path(key).name or key or "(no folder)",
                "path": _display_path(key) if key else "(no folder)",
                "sessions": len(sessions),
                "shown": len(shown),
                "running": sum(c["state"] == "running" for c in mains),
                "waiting": sum(c["state"] == "waiting_for_input" for c in mains),
                "last_activity_at": max((c["last_activity_at"] or "" for c in mains), default="") or None,
            }
        )
        for cs in shown:
            out.extend(cs)
    clusters.sort(key=lambda c: c["last_activity_at"] or "", reverse=True)
    return {"now": now_card, "clusters": clusters, "cards": out, "pending_events": lag(store)}


def map_cards(store: Store, session_ids: list[str]) -> dict[str, Any]:
    now = datetime.now().astimezone()
    with store.reader.connect() as conn:
        rows = _rows(conn, session_ids)
        now_card = _now_card(conn, now)
    cwd_of = {r.session_id: r.execution_cwd for r in rows}
    out = []
    for sid, cards in _cards(rows, now).items():
        for c in cards:
            c["cluster"] = cluster_of(cwd_of[sid])
        out.extend(cards)
    return {"now": now_card, "cards": out, "pending_events": lag(store)}


def session_activity(store: Store, session_id: str) -> dict[str, Any] | None:
    with store.reader.connect() as conn:
        s = conn.execute(select(SessionRow.execution_cwd).where(SessionRow.session_id == session_id)).first()
        if s is None:
            return None
        lanes = conn.execute(
            select(SessionActivity)
            .where(SessionActivity.session_id == session_id)
            .order_by(SessionActivity.lane)
        ).all()
    return {
        "session_id": session_id,
        "cluster": cluster_of(s.execution_cwd),
        "lanes": [
            {
                "lane": r.lane,
                "title": r.title,
                "label": r.label,
                "current": {"tool": r.tool, "target": r.target, "at": _iso(r.action_at)} if r.tool else None,
                "todos": json.loads(r.todos),
                "files": json.loads(r.files)[:200],
                "files_count": r.files_count,
                "last_activity_at": _iso(r.last_activity_at),
            }
            for r in lanes
        ],
    }
