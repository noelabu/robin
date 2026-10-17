"""What each session is doing, for the Map: its title, last tool and target, todo list and changed files.

A projector folds committed events into `session_activity` behind a watermark, off the request path, the
way search indexing does. Claude subagent transcripts get their own lane. It understands Claude Code and
Codex transcripts, the host events of sessions Robin runs, and hook events; Pi gets a title only. Every
stored string is secret-scanned and truncated first (TR-070), and the table is rebuilt from the events when
the extractor or the redaction rules change.
"""

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.adapters.history import claude_workflow_file
from robin.events.ingest import read_events, read_payload_json
from robin.redact.scanner import RuleSet, for_data_dir
from robin.runtime import ledger
from robin.store import Store
from robin.store.models import Event, Meta, SessionActivity
from robin.store.models import Session as SessionRow

VERSION = "5"  # bump when extraction changes: the table is rebuilt from the events
CURSOR_KEY = "activity_cursor"
RULES_KEY = "activity_rules"
BATCH = 500
TEXT_LIMIT = 120
TODO_LIMIT = 140
MAX_TODOS = 50
MAX_FILES = 500
TODO_FILES = 20  # files shown under one task
RESULT_LIMIT = 8192  # tool results are read only to learn a new task's id
AWAIT_LIMIT = 65536  # ... or a background job's id, or how a sync subagent ended
AWAIT_TTL = timedelta(hours=1)  # stop waiting for a result that never came (or was too big to read)
NOTE_LIMIT = 262_144  # notifications and journal lines can embed a workflow's whole result
MAX_JOBS = 200
MAX_RUNS = 20
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
    "tool.finished",
    "tool.progress",
)
PAYLOAD_TYPES = {
    "message.user",
    "tool.started",
    "file.change",
    "session.turn_started",
    "session.plan_updated",
    "tool.finished",
    "tool.progress",
}
EDIT_TOOLS = {"Edit": "edit", "MultiEdit": "edit", "Write": "write", "NotebookEdit": "edit"}
STATUSES = {"pending": "pending", "in_progress": "in_progress", "inProgress": "in_progress",
            "completed": "completed", "done": "completed"}  # fmt: skip
WRAPPED = re.compile(r"<([A-Za-z][\w-]*)\b[^>]*>.*?</\1>", re.S)  # <system-reminder>…</system-reminder>
PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(</pasted_content>|$)", re.S)  # may be cut off unclosed
COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
NOTE_FIELD = re.compile(r"<(task-id|tool-use-id|status|summary|event)>(.*?)</\1>", re.S)
STOPPED_IDS = re.compile(r"Task ids?: ([\w, -]+)")
NOTE_STATUS = {"completed": "completed", "failed": "failed", "killed": "killed", "stopped": "killed"}
PATCH_LINE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


@dataclass
class Lane:
    title: str | None = None
    prompt: str | None = None  # the cleaned prompt in full, for the conversation; never stored
    label: str | None = None
    tool: str | None = None
    target: str | None = None
    action_at: datetime | None = None
    turn_started_at: datetime | None = None
    last_activity_at: datetime | None = None
    todos: list[dict[str, Any]] = field(default_factory=list)  # {id, text, status, files?}
    files: list[dict[str, Any]] = field(default_factory=list)  # {path, op, at, n}, most recent first
    pending: dict[str, str] = field(default_factory=dict)  # TaskCreate tool_use id -> local task id
    # Background work: tool_use id -> job (skill, workflow, shell, monitor, agent), and the results still
    # to read for them (tool_use id -> when it was called).
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    awaiting: dict[str, str] = field(default_factory=dict)
    skill: str | None = None  # the job key of the skill the current prompt is running, if any
    cmd: str | None = None  # a slash command waiting for its skill body
    runs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # workflow run -> agent -> state
    link: dict[str, Any] | None = None  # a subagent: the Agent call or workflow run that started it


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


def _title(lane: Lane, ctx: Ctx, text: Any) -> bool:
    """Take a real prompt as the lane's title and current turn; says whether it was one."""
    if not isinstance(text, str):
        return False
    text = PASTED.sub(" [pasted] ", WRAPPED.sub("", text)).strip()
    if not text or text.startswith("<"):  # command wrappers, caveats, injected context
        return False
    lane.turn_started_at = ctx.at
    lane.prompt = text
    if lane.title is None:
        lane.title = summarize(ctx.rules, text)
    return True


def _touch_file(files: list[dict[str, Any]], rel: str, op: str, at: str, cap: int) -> list[dict[str, Any]]:
    """Move the file to the front with a fresh time, counting repeated edits."""
    seen = next((f for f in files if f["path"] == rel), None)
    entry = {"path": rel, "op": op, "at": at, "n": (seen.get("n", 1) + 1) if seen else 1}
    return [entry, *(f for f in files if f["path"] != rel)][:cap]


def _files(lane: Lane, ctx: Ctx, paths: list[tuple[str, str]]) -> None:
    # An edit is credited to the task in progress at the time, which is what agenttrail-style task
    # panels show under each task. Agents that never mark a task in progress leave it unattributed.
    current = next((t for t in lane.todos if t["status"] == "in_progress"), None)
    for path, op in paths:
        if not path:
            continue
        rel = _rel(ctx, path)
        at = ctx.at.isoformat()
        lane.files = _touch_file(lane.files, rel, op, at, MAX_FILES)
        if current is not None:
            current["files"] = _touch_file(current.get("files", []), rel, op, at, TODO_FILES)


def _replace_todos(lane: Lane, todos: list[dict[str, Any]]) -> None:
    """A new list (TodoWrite, update_plan) keeps each surviving task's files, matched by its text."""
    kept = {t["text"]: t["files"] for t in lane.todos if t.get("files")}
    for t in todos:
        if t["text"] in kept:
            t["files"] = kept[t["text"]]
    lane.todos = todos


def _todo(ctx: Ctx, tid: str, text: Any, status: Any) -> dict[str, Any]:
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
    if call_id:
        _job(lane, ctx, name, inp, call_id)
    edited = inp.get("file_path", inp.get("notebook_path"))
    if name in EDIT_TOOLS and isinstance(edited, str):
        _files(lane, ctx, [(edited, EDIT_TOOLS[name])])
    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
        _replace_todos(
            lane,
            [
                _todo(ctx, str(i), t.get("content"), t.get("status"))
                for i, t in enumerate(inp["todos"][:MAX_TODOS], 1)
                if isinstance(t, dict)
            ],
        )
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


def _add_job(lane: Lane, ctx: Ctx, key: str, job: dict[str, Any], awaiting: bool = True) -> None:
    job.update(status=None, started_at=ctx.at.isoformat(), ended_at=None)
    lane.jobs[key] = job
    if awaiting:
        lane.awaiting[key] = ctx.at.isoformat()
    if len(lane.jobs) > MAX_JOBS:  # drop the oldest finished job, else the oldest
        drop = next((k for k, j in lane.jobs.items() if j["status"]), next(iter(lane.jobs)))
        del lane.jobs[drop]
        lane.awaiting.pop(drop, None)


def _job(lane: Lane, ctx: Ctx, name: str, inp: dict[str, Any], call_id: str) -> None:
    """Skills, workflows, subagents and background shells become jobs the Map shows as their own cards."""
    open_skill = lane.skill if lane.skill in lane.jobs else None

    def text(key: str, limit: int = TEXT_LIMIT) -> str | None:
        return summarize(ctx.rules, inp[key], limit) if isinstance(inp.get(key), str) else None

    if name == "Skill" and isinstance(inp.get("skill"), str):
        if open_skill:  # a skill calling another: one card for the chain, named by the outermost
            inner = lane.jobs[open_skill].setdefault("inner", [])
            skill = summarize(ctx.rules, inp["skill"], 60)
            if skill not in inner and len(inner) < 5:
                inner.append(skill)
            return
        _add_job(
            lane, ctx, call_id, {"kind": "skill", "name": text("skill", 60), "args": text("args")}, False
        )
        lane.skill = call_id
    elif name in ("Agent", "Task") and ("prompt" in inp or "subagent_type" in inp):
        _add_job(
            lane,
            ctx,
            call_id,
            {
                "kind": "agent",
                "name": text("subagent_type", 40),
                "title": text("description"),
                "parent": open_skill,
            },
        )
    elif name == "Workflow":
        script = inp.get("scriptPath")
        wf = (
            inp.get("name")
            if isinstance(inp.get("name"), str)
            else Path(script).stem
            if isinstance(script, str)
            else None
        )
        _add_job(
            lane,
            ctx,
            call_id,
            {"kind": "workflow", "name": summarize(ctx.rules, wf, 60) if wf else None, "parent": open_skill},
        )
    elif (name == "Bash" and inp.get("run_in_background") is True) or name == "Monitor":
        command = inp.get("command")
        first = command.strip().splitlines()[0] if isinstance(command, str) and command.strip() else None
        _add_job(
            lane,
            ctx,
            call_id,
            {
                "kind": "shell" if name == "Bash" else "monitor",
                "title": text("description") or (summarize(ctx.rules, first) if first else None),
                "command": summarize(ctx.rules, command, 300) if isinstance(command, str) else None,
                "parent": open_skill,
            },
        )
    elif name in ("TaskStop", "KillShell"):
        task = inp.get("task_id", inp.get("shell_id"))
        for job in lane.jobs.values():
            if task is not None and job.get("task") == str(task) and not job["status"]:
                job.update(status="killed", ended_at=ctx.at.isoformat())


def _note(text: str) -> dict[str, str]:
    """The fields of a <task-notification>."""
    return {m.group(1): html.unescape(m.group(2).strip()) for m in NOTE_FIELD.finditer(text[:NOTE_LIMIT])}


def _notify(lane: Lane, ctx: Ctx, text: str) -> None:
    """Background work finished (or a monitor saw an event). Repeats of the same notification are harmless."""
    note = _note(text)
    jobs = []
    if note.get("tool-use-id") in lane.jobs:
        jobs = [lane.jobs[note["tool-use-id"]]]
    elif note.get("task-id"):
        jobs = [j for j in lane.jobs.values() if j.get("task") == note["task-id"]]
    elif note.get("status") == "stopped":  # the session ended under them: "Task ids: a, b"
        m = STOPPED_IDS.search(note.get("summary", ""))
        ids = {x.strip() for x in m.group(1).split(",")} if m else set()
        jobs = [j for j in lane.jobs.values() if j.get("task") in ids]
    status = NOTE_STATUS.get(note.get("status", ""))
    for job in jobs:
        if status:
            job.update(status=status, ended_at=ctx.at.isoformat())
            if note.get("summary"):
                job["summary"] = summarize(ctx.rules, note["summary"])
        elif note.get("event"):
            job["event"] = summarize(ctx.rules, note["event"])


def _journal(lane: Lane, run: str, rec: Any) -> None:
    """A workflow run's journal line: an agent started in a phase, or ended."""
    if not isinstance(rec, dict) or not isinstance(rec.get("agentId"), str):
        return
    agents = lane.runs.setdefault(run, {})
    while len(lane.runs) > MAX_RUNS:
        del lane.runs[next(iter(lane.runs))]
    agent = agents.setdefault(rec["agentId"], {"phase": None, "label": None, "status": None})
    if rec.get("type") == "started":
        agent["phase"] = str(rec["phase"])[:60] if rec.get("phase") else None
        agent["label"] = str(rec["label"])[:120] if rec.get("label") else None
    elif rec.get("type") == "result":
        agent["status"] = "completed"
    elif rec.get("type") == "failed":
        agent["status"] = "failed"


def _plan(lane: Lane, ctx: Ctx, plan: list[Any]) -> None:
    _replace_todos(
        lane,
        [
            _todo(ctx, str(i), t.get("step"), t.get("status"))
            for i, t in enumerate(plan[:MAX_TODOS], 1)
            if isinstance(t, dict)
        ],
    )


def _job_result(lane: Lane, ctx: Ctx, b: dict[str, Any], tur: Any) -> None:
    """A job's launch result: the id its notification will use, a workflow's run, or a sync agent's end."""
    job = lane.jobs.get(b["tool_use_id"])
    lane.awaiting.pop(b["tool_use_id"], None)
    if job is None:
        return
    tur = tur if isinstance(tur, dict) else {}
    task = tur.get("backgroundTaskId") or tur.get("taskId") or tur.get("agentId")
    if task:
        job["task"] = str(task)[:80]
    if job["kind"] == "workflow":
        if tur.get("runId"):
            job["run"] = str(tur["runId"])[:80]
        if tur.get("workflowName"):
            job["name"] = summarize(ctx.rules, tur["workflowName"], 60)
    if b.get("is_error"):
        job.update(status="failed", ended_at=ctx.at.isoformat())
    elif job["kind"] == "agent" and tur.get("status") != "async_launched":
        job.update(status="completed", ended_at=ctx.at.isoformat())


def _task_result(lane: Lane, rec: dict[str, Any], ctx: Ctx | None = None) -> None:
    """A TaskCreate result names the task's real id; renumber the todo created for it."""
    blocks = (rec.get("message") or {}).get("content")
    for b in blocks if isinstance(blocks, list) else []:
        if (
            ctx
            and isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("tool_use_id") in lane.awaiting
        ):
            _job_result(lane, ctx, b, rec.get("toolUseResult"))
            continue
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
    if event_type == "tool.finished":  # a notification only seen in Claude's queue records
        note = (
            (payload.get("attachment") or {}).get("prompt")
            if kind == "attachment"
            else payload.get("content")
        )
        if isinstance(note, str):
            _notify(lane, ctx, note)
        return
    if kind in ("assistant", "user") and isinstance(payload.get("message"), dict):  # Claude transcript
        content = payload["message"].get("content")
        if kind == "user":
            if event_type == "tool.result":
                _task_result(lane, payload, ctx)
                return
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
            if not isinstance(content, str):
                return
            if content.lstrip().startswith("<task-notification>"):
                _notify(lane, ctx, content)
            elif payload.get("isMeta"):
                if lane.cmd and lane.skill is None and content.startswith("Base directory for this skill"):
                    key = f"cmd-{payload.get('uuid') or ctx.at.isoformat()}"
                    _add_job(
                        lane, ctx, key, {"kind": "skill", "name": summarize(ctx.rules, lane.cmd, 60)}, False
                    )
                    lane.skill, lane.cmd = key, None
            elif m := COMMAND.search(content):  # a slash command: a new prompt, maybe a skill
                lane.skill, lane.cmd = None, m.group(1)
            elif _title(lane, ctx, content):
                lane.skill = lane.cmd = None
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


# -- event summaries (the activity timeline) ----------------------------------------------------------

NOISE_TYPES = {
    "unknown.raw",
    "message.delta",
    "tool.output",
    "tool.result",
    "file.read",
    "control.submitted",
    "control.acknowledged",
}
GENERIC = {
    "session.started": "Session started",
    "session.turn_started": "Turn started",
    "session.turn_completed": "Turn completed",
    "session.stopped": "Session stopped",
    "session.idle": "Idle",
    "session.resumed": "Resumed",
    "connection.connected": "Robin connected",
    "connection.reconnecting": "Reconnecting",
    "connection.stale": "Connection stale",
    "connection.recovered": "Reconnected",
    "connection.gap": "Gap in coverage: some events weren't observed",
    "finding.published": "Published a finding",
    "control.outcome_unknown": "Control outcome unknown",
}
NO_PAYLOAD = set(GENERIC) | {"tool.output", "message.delta", "file.read"}


def _first_line(rules: RuleSet, text: Any, limit: int = TEXT_LIMIT) -> str | None:
    """Redact the window first, then take its first line: a line cut before redacting could split a
    multi-line secret. The output limit is far below the 4096-character window."""
    if not isinstance(text, str):
        return None
    masked = rules.redact(text[:4096])[0]
    line = next((x.strip() for x in masked.splitlines() if x.strip()), "")
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _assistant_text(payload: dict[str, Any]) -> str | None:
    kind = payload.get("type")
    if kind == "assistant" and isinstance(payload.get("message"), dict):  # Claude transcript
        blocks = payload["message"].get("content")
        if isinstance(blocks, str):
            return blocks
        texts = [b.get("text", "") for b in blocks or [] if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) or None
    if kind == "event_msg" and isinstance(payload.get("payload"), dict):  # Codex
        p = payload["payload"]
        return p.get("message") if p.get("type") == "agent_message" else None
    if kind == "message" and isinstance(payload.get("message"), dict):  # Pi transcript
        content = payload["message"].get("content")
        if isinstance(content, list):
            return "\n".join(c.get("text", "") for c in content if isinstance(c, dict)) or None
        return content if isinstance(content, str) else None
    text = payload.get("text")  # host events
    return text if isinstance(text, str) else None


def summary(
    rules: RuleSet, cwd: str | None, runtime: str, event_type: str, payload: Any, at: datetime
) -> tuple[str | None, bool]:
    """One readable, redacted line for an event, and whether it's noise (hidden by default)."""
    if event_type in GENERIC:
        return GENERIC[event_type], False
    if isinstance(payload, dict) and payload.get("_robin_pruned"):
        return "Pruned to stay under the storage cap; no longer available.", False
    if not isinstance(payload, dict):
        return None, True
    name = runtime.capitalize()
    ctx = Ctx(rules, cwd, at)
    lane = Lane()
    if event_type == "message.user":
        if payload.get("type") == "response_item":  # Codex repeats the prompt as an event_msg
            return None, True
        fold(lane, ctx, event_type, payload)
        return (f"You: {lane.title}", False) if lane.title else (None, True)
    if event_type == "message.completed":
        if payload.get("type") == "response_item":
            return None, True
        line = _first_line(rules, _assistant_text(payload))
        return (f"{name}: {line}", False) if line else (None, True)
    if event_type == "tool.started":
        fold(lane, ctx, event_type, payload)
        if lane.tool:
            return (f"{lane.tool} {lane.target}" if lane.target else lane.tool), False
        if lane.todos:
            return (
                f"Plan: {sum(t['status'] == 'completed' for t in lane.todos)} of {len(lane.todos)} done",
                False,
            )
        return None, True
    if event_type == "tool.error":
        tool = summarize(rules, payload.get("tool") or "Tool", 60)
        line = _first_line(rules, payload.get("output") or payload.get("error") or "")
        return (f"{tool} failed: {line}" if line else f"{tool} failed"), False
    if event_type == "file.change":
        fold(lane, ctx, event_type, payload)
        names = [Path(f["path"]).name for f in lane.files]
        if not names:
            return None, True
        more = f" (+{len(names) - 2})" if len(names) > 2 else ""
        return f"Changed {', '.join(names[:2])}{more}", False
    if event_type == "session.plan_updated":
        fold(lane, ctx, event_type, payload)
        return f"Plan: {sum(t['status'] == 'completed' for t in lane.todos)} of {len(lane.todos)} done", False
    if event_type == "session.failed":
        line = _first_line(rules, payload.get("reason") or payload.get("message") or payload.get("error"))
        return (f"Failed: {line}" if line else "Failed"), False
    if event_type == "attention.approval_requested":
        approve = payload.get("tool")
        target = _target(ctx, str(approve), payload.get("input")) if approve else None
        what = " ".join(x for x in (summarize(rules, approve, 60) if approve else None, target) if x)
        return f"Approval requested: {what}" if what else "Approval requested", False
    if event_type == "attention.question":
        line = _first_line(rules, payload.get("text") or payload.get("question"))
        return (f"Question: {line}" if line else "Question"), False
    if event_type == "control.rejected":
        line = _first_line(rules, payload.get("reason"))
        return (f"Rejected: {line}" if line else "Rejected"), False
    if event_type in NOISE_TYPES:
        return None, True
    return None, True


CONVERSATION_EVENTS = 5000  # the newest events a conversation is built from
MESSAGE_LIMIT = 100_000  # characters of one message shown in full


def full_text(rules: RuleSet, text: str) -> str:
    """A whole message, redacted before it's cut so a secret can't straddle the cut."""
    masked = rules.redact(text[: MESSAGE_LIMIT + 4096])[0].strip()
    return masked if len(masked) <= MESSAGE_LIMIT else masked[:MESSAGE_LIMIT] + "\n\n… (truncated)"


def conversation(store: Store, session_id: str) -> list[dict[str, Any]]:
    return conversation_page(store, session_id)[0]


def conversation_page(store: Store, session_id: str) -> tuple[list[dict[str, Any]], int]:
    """The session as a chat: the user's prompts and the assistant's replies in full (redacted), with
    the tool calls between them grouped as one-line summaries. Oldest first. Only the main thread:
    subagents' own prompts and reports stay on their cards.

    Also the cursor it covers: every later event of the session has a larger one, so a stream read after it
    neither misses nor repeats one."""
    rules = for_data_dir(store.data_dir)
    with store.reader.connect() as conn:
        cwd = conn.execute(
            select(SessionRow.execution_cwd).where(SessionRow.session_id == session_id)
        ).scalar()
    with store.reader.connect() as conn:
        head = conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one()
    events = read_events(store, session_id=session_id, limit=CONVERSATION_EVENTS, latest=True)
    out: list[dict[str, Any]] = []

    def prompt(text: str, at: datetime) -> None:
        text = full_text(rules, text)
        # A hook and the transcript both report a prompt, with at most tool calls between the two
        # reports; the same words sent again after a reply are a new prompt.
        last = next((m for m in reversed(out) if m["role"] != "tools"), None)
        if last is None or last["role"] != "user" or last["text"] != text:
            out.append({"role": "user", "at": at.isoformat(), "text": text})

    # Prompts sent through Robin: runtimes don't always echo them back as events.
    with store.reader.connect() as conn:
        sent = [
            (a.requested_at, str(a.payload.get("text") or a.payload.get("prompt") or ""))
            for a in ledger.for_session(conn, session_id, ("launch", "message", "takeover"))
            if a.state not in ("failed", "cancelled") and (a.payload.get("text") or a.payload.get("prompt"))
        ]
    if events:  # only what falls inside the window of events shown
        first = events[0].source_time or events[0].ingested_at
        sent = [(at, t) for at, t in sent if at >= first]
    for e in events:
        at = e.source_time or e.ingested_at
        while sent and sent[0][0] <= at:
            prompt(sent[0][1], sent[0][0])
            sent.pop(0)
        if e.event_type not in ("message.user", "message.completed", "tool.started", "tool.error"):
            continue
        if lane_of(e.source_id):
            continue
        at = e.source_time or e.ingested_at
        try:
            payload = read_payload_json(store, e.global_cursor)
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict) or payload.get("type") == "response_item":  # Codex repeats
            continue
        when = at.isoformat()
        if e.event_type == "message.user":
            lane = Lane()
            fold(lane, Ctx(rules, cwd, at), e.event_type, payload)
            if lane.prompt:
                prompt(lane.prompt, at)
        elif e.event_type == "message.completed":
            reply = _assistant_text(payload)
            if not reply or not reply.strip():
                continue
            if out and out[-1]["role"] == "assistant":  # one reply split over several records
                out[-1]["text"] = full_text(rules, out[-1]["text"] + "\n\n" + reply)
            else:
                out.append({"role": "assistant", "at": when, "text": full_text(rules, reply)})
        else:
            line, noise = summary(rules, cwd, e.runtime, e.event_type, payload, at)
            if not line or noise:
                continue
            if out and out[-1]["role"] == "tools":
                if out[-1]["items"][-1] != line:  # a hook and the transcript both report a call
                    out[-1]["items"].append(line)
            else:
                out.append({"role": "tools", "at": when, "items": [line]})
    for at, text in sent:  # sent after the newest event
        prompt(text, at)
    return out, max(head, events[-1].global_cursor if events else 0)


def summaries(store: Store, events: list[Any]) -> list[tuple[str | None, bool]]:
    """Summaries for a list of events (anything with global_cursor, session_id, runtime, event_type,
    payload_ref and a time). Payloads are read only where the summary needs them."""
    rules = for_data_dir(store.data_dir)
    ids = {e.session_id for e in events}
    with store.reader.connect() as conn:
        cwd = dict(
            conn.execute(
                select(SessionRow.session_id, SessionRow.execution_cwd).where(SessionRow.session_id.in_(ids))
            ).all()
        )
    out: list[tuple[str | None, bool]] = []
    for e in events:
        at = e.source_time or e.ingested_at
        if e.event_type in NO_PAYLOAD:
            out.append(summary(rules, None, e.runtime, e.event_type, None, at))
            continue
        if e.payload_ref is not None:  # large payloads stay behind their reference
            text = (
                f"{e.runtime.capitalize()} replied (long message)"
                if e.event_type == "message.completed"
                else None
            )
            out.append((text, text is None))
            continue
        try:
            payload = read_payload_json(store, e.global_cursor)
        except (ValueError, OSError):
            payload = None
        out.append(summary(rules, cwd.get(e.session_id), e.runtime, e.event_type, payload, at))
    return out


# -- lanes ---------------------------------------------------------------------------------------------------


def lane_of(source_id: str) -> str:
    """Claude subagents live in <session>/subagents/agent-*.jsonl, and workflow agents in
    <session>/subagents/workflows/<run>/agent-*.jsonl; both are tailed into their parent session. A run's
    journal folds into the main lane, next to the Workflow call that started it."""
    if source_id.startswith("file:"):
        path = Path(source_id[5:])
        if path.parent.name == "subagents" or (run_of(source_id) and path.stem != "journal"):
            return path.stem
    return ""


def run_of(source_id: str) -> str | None:
    path = Path(source_id[5:]) if source_id.startswith("file:") else None
    return path.parent.name if path and claude_workflow_file(path) else None


def _subagent_meta(rules: RuleSet, source_id: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """Read once, when the lane is created: by then the meta file exists, and on a rebuild it is final."""
    link: dict[str, Any] = {"run": run_of(source_id)}
    meta = Path(source_id[5:]).with_suffix(".meta.json")
    try:
        raw = json.loads(meta.read_text()) if meta.stat().st_size < 65536 else {}
    except (OSError, ValueError):
        return None, None, link
    if not isinstance(raw, dict):
        return None, None, link
    label = summarize(rules, raw["agentType"], 40) if isinstance(raw.get("agentType"), str) else None
    title = summarize(rules, raw["description"]) if isinstance(raw.get("description"), str) else None
    if isinstance(raw.get("toolUseId"), str):
        link["tool_use_id"] = raw["toolUseId"][:80]
    if isinstance(raw.get("workflowPhase"), str):
        link["phase"] = summarize(rules, raw["workflowPhase"], 60)
    link["finished"] = raw.get("finished") is True
    return label, title, link


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
        jobs=scratch.get("jobs", {}),
        awaiting=scratch.get("awaiting", {}),
        skill=scratch.get("skill"),
        cmd=scratch.get("cmd"),
        runs=scratch.get("runs", {}),
        link=scratch.get("link"),
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
        scratch=json.dumps(
            {
                "pending": lane.pending,
                "jobs": lane.jobs,
                "awaiting": lane.awaiting,
                "skill": lane.skill,
                "cmd": lane.cmd,
                "runs": lane.runs,
                "link": lane.link,
            }
        ),
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
                lane.label, lane.title, lane.link = _subagent_meta(rules, r.source_id)
        lanes[key] = lane
        ctx = Ctx(rules, r.execution_cwd, r.source_time or r.ingested_at)
        if lane.awaiting:
            lane.awaiting = {
                k: v for k, v in lane.awaiting.items() if ctx.at - datetime.fromisoformat(v) < AWAIT_TTL
            }
        wanted = (
            (
                r.event_type in PAYLOAD_TYPES
                and (r.event_type not in ("tool.finished", "tool.progress") or r.payload_size < NOTE_LIMIT)
            )
            or (r.event_type == "tool.result" and lane.pending and r.payload_size < RESULT_LIMIT)
            or (r.event_type == "tool.result" and lane.awaiting and r.payload_size < AWAIT_LIMIT)
        )
        payload = None
        if wanted:
            try:
                payload = read_payload_json(store, r.global_cursor)
            except (ValueError, OSError):
                payload = None
        if r.event_type == "tool.progress":
            _journal(lane, run_of(r.source_id) or "", payload)
            continue
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
SUBAGENTS_SHOWN = 8  # per parent: the session, a skill or a workflow
SHELL_SHOWN_S = 600  # a finished background shell keeps its card this long, then folds into a count
GROUP_SHOWN_S = 1800  # likewise a finished skill or workflow, with everything under it
SHELL_IDLE_S = 1800  # a shell with no end signal counts as running this long after the session's activity
STATUS_STATE = {"completed": "idle", "failed": "failed", "killed": "stopped"}
JOB_LABEL = {"skill": "Skill", "workflow": "Workflow", "shell": "Background shell", "monitor": "Monitor"}


def _now() -> datetime:
    return datetime.now().astimezone()


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


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _card(s: Any, lane: Any, now: datetime, parent: str | None = None) -> dict[str, Any]:
    last = lane.last_activity_at if lane is not None else None  # not derived yet: unknown, not "just now"
    state, evidence = _state(None if parent else s.runtime_state, last, now)
    if parent and s.runtime_state in ("stopped", "failed"):
        state, evidence = "stopped", "inferred"
    total = lane.todo_total if lane is not None else 0
    return {
        "id": f"{s.session_id}:{lane.lane}" if parent else s.session_id,
        "kind": "subagent" if parent else "session",
        "ref": lane.lane if lane is not None else "",
        "status": None,
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
        "skip_permissions": not parent and bool(s.skip_permissions),
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
            SessionRow.skip_permissions,
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
            SessionActivity.scratch,
        )
        .outerjoin(SessionActivity, SessionActivity.session_id == SessionRow.session_id)
        .where(SessionRow.archived.is_(False))
    )
    if session_ids is not None:
        q = q.where(SessionRow.session_id.in_(session_ids))
    return list(conn.execute(q).all())


def _job_card(s: Any, key: str, job: dict[str, Any]) -> dict[str, Any]:
    kind = "shell" if job["kind"] == "monitor" else job["kind"]
    return {
        "id": f"{s.session_id}:{key}",
        "kind": kind,
        "ref": key,
        "status": job.get("status"),
        "session_id": s.session_id,
        "parent": s.session_id,
        "runtime": s.runtime,
        "mode": s.mode,
        "label": JOB_LABEL[job["kind"]],
        "title": job.get("name") if kind in ("skill", "workflow") else job.get("title"),
        "state": "idle",
        "state_evidence": "inferred",
        "detail": True,
        "todos": None,
        "files": 0,
        "last_file": None,
        "tool": None,
        "target": job.get("command"),
        "action_at": None,
        "turn_started_at": None,
        "last_activity_at": job.get("ended_at") or job.get("started_at"),
        "created_at": job.get("started_at"),
        "subagents_hidden": 0,
        "args": job.get("args"),
        "inner": job.get("inner", []),
        "summary": job.get("summary") or job.get("event"),
        "run": job.get("run"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
    }


def _reported(card: dict[str, Any], status: str | None) -> None:
    if status in STATUS_STATE:
        card.update(status=status, state=STATUS_STATE[status], state_evidence="reported")


def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), key=lambda x: _when(x) or datetime.min, default=None)


def _session_cards(s: Any, main: Any, subs: list[Any], now: datetime) -> list[dict[str, Any]]:
    """A session's card, its skills, workflows and background shells, and its subagents under whichever
    of those started them. Every card is returned; a finished one past its time, and the subagents past a
    parent's cap, are marked "hidden" ("folded" or "capped")."""
    sid = s.session_id
    scratch = json.loads(main.scratch or "{}") if main is not None else {}
    jobs: dict[str, dict[str, Any]] = scratch.get("jobs", {})
    runs: dict[str, dict[str, Any]] = scratch.get("runs", {})
    card = _card(s, main, now)
    if main is None and subs:  # only subagents seen so far: the parent still has activity
        card["last_activity_at"] = (
            _iso(max((x.last_activity_at for x in subs if x.last_activity_at), default=None))
            or card["last_activity_at"]
        )
    card.update(shells_done=0, groups_done=0)
    groups = {k: _job_card(s, k, j) for k, j in jobs.items() if j["kind"] != "agent"}
    for k, c in groups.items():
        parent = jobs[k].get("parent")
        if parent in groups and c["kind"] != "skill":
            c["parent"] = groups[parent]["id"]
    run_card = {c["run"]: c for c in groups.values() if c["kind"] == "workflow" and c["run"]}

    agents = []
    for x in subs:
        c = _card(s, x, now, parent=sid)
        link = json.loads(x.scratch or "{}").get("link") or {}
        status = None
        if link.get("run"):
            agent = runs.get(link["run"], {}).get(x.lane.removeprefix("agent-")) or {}
            status = agent.get("status")
            c["phase"] = agent.get("phase") or link.get("phase")
            if link["run"] in run_card:
                c["parent"] = run_card[link["run"]]["id"]
        elif link.get("tool_use_id") in jobs:
            job = jobs[link["tool_use_id"]]
            status = job["status"]
            ended = _when(job.get("ended_at"))
            if status and ended and x.last_activity_at and x.last_activity_at > ended:
                status = None  # resumed after it ended (SendMessage): working again
            if job.get("parent") in groups:
                c["parent"] = groups[job["parent"]]["id"]
        if not status and link.get("finished"):
            status = "completed"
        _reported(c, status)
        agents.append(c)

    def children(c: dict[str, Any]) -> list[dict[str, Any]]:
        return [x for x in [*agents, *groups.values()] if x["parent"] == c["id"]]

    session_last = _when(card["last_activity_at"])
    for c in groups.values():
        if c["kind"] != "shell":
            continue
        if c["status"]:
            _reported(c, c["status"])
        elif s.runtime_state in ("stopped", "failed"):
            c["state"] = "stopped"
        elif session_last and (now - session_last).total_seconds() < SHELL_IDLE_S:
            c["state"] = "running"
    for kind in ("workflow", "skill"):  # workflows first: a skill's state depends on its workflows'
        for c in groups.values():
            if c["kind"] != kind:
                continue
            kids = children(c)
            for k in kids:
                c["last_activity_at"] = _later(c["last_activity_at"], k["last_activity_at"])
            if kind == "workflow":
                phases: dict[str, dict[str, Any]] = {}
                for agent in runs.get(c["run"] or "", {}).values() or [
                    {"phase": k.get("phase"), "status": k["status"]} for k in kids
                ]:
                    ph = phases.setdefault(
                        agent["phase"] or "",
                        {"name": agent["phase"] or "", "done": 0, "failed": 0, "total": 0},
                    )
                    ph["total"] += 1
                    ph["done"] += agent["status"] == "completed"
                    ph["failed"] += agent["status"] == "failed"
                c["phases"] = list(phases.values())
            if c["status"]:
                _reported(c, c["status"])
            elif any(k["state"] == "failed" for k in kids):
                c.update(state="failed", status="failed")
            elif (
                any(k["state"] in ("running", "waiting_for_input") for k in kids)
                or (kind == "skill" and scratch.get("skill") == c["ref"] and card["state"] == "running")
                or (kind == "workflow" and _state(None, _when(c["started_at"]), now)[0] == "running")
            ):
                c["state"] = "running"
            elif kind == "skill" or kids:
                c["status"] = "completed"

    # Fold what finished a while ago; failures stay.
    def fold_under(c: dict[str, Any]) -> None:
        for k in children(c):
            k["hidden"] = "folded"
            fold_under(k)

    for c in groups.values():
        ended = _when(c["last_activity_at"])
        age = (now - ended).total_seconds() if ended else 0
        if c["parent"] != sid or c["state"] in ("running", "waiting_for_input", "failed"):
            continue
        if c["kind"] == "shell" and age > SHELL_SHOWN_S:
            c["hidden"] = "folded"
            card["shells_done"] += 1
        elif c["kind"] != "shell" and age > GROUP_SHOWN_S:
            c["hidden"] = "folded"
            card["groups_done"] += 1
            fold_under(c)

    # Cap each parent's subagents: the working and failed ones first, then the newest.
    oldest = datetime.min.replace(tzinfo=now.tzinfo)
    by_id = {c["id"]: c for c in [card, *groups.values()]}
    for parent in by_id.values():
        kids = [a for a in agents if a["parent"] == parent["id"] and not a.get("hidden")]
        kids.sort(
            key=lambda a: (
                a["state"] in ("running", "waiting_for_input", "failed"),
                _when(a["last_activity_at"]) or oldest,
            ),
            reverse=True,
        )
        for a in kids[SUBAGENTS_SHOWN:]:
            a["hidden"] = "capped"
        parent["subagents_hidden"] = max(0, len(kids) - SUBAGENTS_SHOWN)
    rest = sorted(
        [*groups.values(), *agents], key=lambda c: _when(c["last_activity_at"]) or oldest, reverse=True
    )
    return [card, *rest]


def _by_session(rows: list[Any]) -> dict[str, tuple[Any, Any, list[Any]]]:
    by_session: dict[str, tuple[Any, Any, list[Any]]] = {}
    for r in rows:
        s, main, subs = by_session.get(r.session_id, (r, None, []))
        if r.lane == "":
            main = r
        elif r.lane:
            subs.append(r)
        by_session[r.session_id] = (s, main, subs)
    return by_session


def _cards(rows: list[Any], now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Session id -> [its card, then the cards shown under it]."""
    return {
        sid: [c for c in _session_cards(s, main, subs, now) if not c.get("hidden")]
        for sid, (s, main, subs) in _by_session(rows).items()
    }


def map_snapshot(store: Store, per_cluster: int = 50, cluster: str | None = None) -> dict[str, Any]:
    now = _now()
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
    now = _now()
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
        rows = _rows(conn, [session_id])
    cards = []
    for _sid, (row, main, subs) in _by_session(rows).items():
        cards = _session_cards(row, main, subs, _now())
    agents = {c["ref"]: c for c in cards if c["kind"] == "subagent"}
    main_scratch = next((json.loads(r.scratch or "{}") for r in lanes if r.lane == ""), {})
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
                "status": agents[r.lane]["status"] if r.lane in agents else None,
                "phase": agents[r.lane].get("phase") if r.lane in agents else None,
                "parent": agents[r.lane]["parent"] if r.lane in agents else None,
            }
            for r in lanes
        ],
        # Skills, workflows and background shells, folded ones included, newest first.
        "jobs": [
            {**c, "hidden": c.get("hidden")} for c in cards if c["kind"] in ("skill", "workflow", "shell")
        ],
        "runs": main_scratch.get("runs", {}),
    }
