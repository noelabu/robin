"""Opt-in hook installers (FR-041, TR-076).

Claude Code (`hooks` in ~/.claude/settings.json) and Codex (~/.codex/hooks.json) share a config shape:
`{"hooks": {"<Event>": [{"matcher"?: str, "hooks": [{"type": "command", "command": str, "timeout": int}]}]}}`.
Robin appends its own group to each event list and never edits, replaces or reorders anyone else's. Pi's
mechanism is an extension file, so Robin adds one file of its own.

For Claude, Robin also adds its question hook: one more PreToolUse group, matched to AskUserQuestion, that
waits for the question to be answered in Robin (robin.hooks.ask). It's the only hook that decides anything.

Every install is planned first (a diff of exactly what changes), written only when confirmed, backed up,
and recorded, so uninstall removes only Robin's entries and status can report drift.
"""

import difflib
import hashlib
import json
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robin.ids import utc_now
from robin.paths import secure_mkdir

RELAY_MODULE = "robin.hooks.relay"
HOOK_TIMEOUT_S = 5
ASK_MODULE = "robin.hooks.ask"
ASK_EVENT, ASK_MATCHER = "PreToolUse", "AskUserQuestion"
ASK_TIMEOUT_S = 600  # above the longest wait (attention.WAIT_MAX_S), so Claude never kills it mid-wait
EVENTS = {
    "claude": [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
        "Notification",
        "SessionEnd",
    ],
    "codex": [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    ],
}
PI_FILE = "robin-observer.ts"
PI_TEMPLATE = Path(__file__).with_name("pi_observer.ts")


class InstallError(Exception):
    pass


@dataclass
class Plan:
    runtime: str
    target: Path
    action: str  # install | uninstall | nothing
    before: str | None
    after: str | None
    notes: list[str] = field(default_factory=list)
    created_events: list[str] = field(default_factory=list)

    def diff(self) -> str:
        a = (self.before or "").splitlines(keepends=True)
        b = (self.after or "").splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, f"{self.target} (now)", f"{self.target} (after)"))


def config_path(runtime: str) -> Path:
    """The harness config Robin would change. Tests point these elsewhere through the environment."""
    env = os.environ.get(f"ROBIN_HOOK_CONFIG_{runtime.upper()}")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if runtime == "claude":
        return home / ".claude" / "settings.json"
    if runtime == "codex":
        return home / ".codex" / "hooks.json"
    if runtime == "pi":
        return home / ".pi" / "agent" / "extensions" / PI_FILE
    raise InstallError(f"no hook installer for {runtime}")


def relay_command(runtime: str, data_dir: Path) -> str:
    """Absolute interpreter and data dir: hooks run with the harness's PATH and environment."""
    return shlex.join([sys.executable, "-m", RELAY_MODULE, "--runtime", runtime, "--data", str(data_dir)])


def ask_command(data_dir: Path) -> str:
    return shlex.join([sys.executable, "-m", ASK_MODULE, "--data", str(data_dir)])


def is_relay(hook: Any) -> bool:
    return isinstance(hook, dict) and RELAY_MODULE in str(hook.get("command", ""))


def is_ask(hook: Any) -> bool:
    return isinstance(hook, dict) and ASK_MODULE in str(hook.get("command", ""))


def is_robin(hook: Any) -> bool:
    return is_relay(hook) or is_ask(hook)


# -- records -----------------------------------------------------------------------------------------------


def _records_path(data_dir: Path) -> Path:
    return data_dir / "hooks" / "installed.json"


def records(data_dir: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(_records_path(data_dir).read_text()))
    except (FileNotFoundError, ValueError):
        return {}


def _save_records(data_dir: Path, recs: dict[str, Any]) -> None:
    secure_mkdir(data_dir / "hooks")
    _atomic_write(_records_path(data_dir), json.dumps(recs, indent=2) + "\n", mode=0o600)


# -- planning ----------------------------------------------------------------------------------------------


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    text = path.read_text()
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise InstallError(f"{path} isn't valid JSON ({exc}); Robin won't edit it") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} isn't a JSON object; Robin won't edit it")
    return data, text


def _indent(text: str | None) -> int:
    for line in (text or "").splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return 2


def _dump(data: dict[str, Any], like: str | None) -> str:
    return json.dumps(data, indent=_indent(like), ensure_ascii=False) + "\n"


def plan_install(runtime: str, data_dir: Path) -> Plan:
    target = config_path(runtime)
    if runtime == "pi":
        body = PI_TEMPLATE.read_text().replace(
            "__ROBIN_RELAY_ARGV__",
            json.dumps([sys.executable, "-m", RELAY_MODULE, "--runtime", "pi", "--data", str(data_dir)]),
        )
        before = target.read_text() if target.exists() else None
        if before == body:
            return Plan(runtime, target, "nothing", before, before, ["already installed"])
        notes = ["replaces Robin's earlier observer file"] if before is not None else []
        return Plan(runtime, target, "install", before, body, notes)

    data, text = _load_json(target)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"`hooks` in {target} isn't an object; Robin won't edit it")
    command = relay_command(runtime, data_dir)
    created: list[str] = []
    changed = False
    new_hooks = dict(hooks)
    for event in EVENTS[runtime]:
        groups = hooks.get(event)
        if groups is not None and not isinstance(groups, list):
            raise InstallError(f"`hooks.{event}` in {target} isn't a list; Robin won't edit it")
        groups = list(groups or [])
        mine = [h for g in groups if isinstance(g, dict) for h in g.get("hooks", []) or [] if is_relay(h)]
        if mine:
            if any(h.get("command") != command for h in mine):  # Robin moved: update its own entries only
                groups = [_with_command(g, command) for g in groups]
                new_hooks[event] = groups
                changed = True
            continue
        if event not in hooks:
            created.append(event)
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]})
        new_hooks[event] = groups
        changed = True
    if runtime == "claude":
        groups, ask_changed = _with_ask(list(new_hooks.get(ASK_EVENT) or []), ask_command(data_dir))
        if ask_changed:
            new_hooks[ASK_EVENT] = groups
            changed = True
    if not changed:
        return Plan(runtime, target, "nothing", text, text, ["already installed"])
    after = dict(data)
    after["hooks"] = new_hooks
    notes = []
    if text is not None and _dump(data, text) != text:
        notes.append("the file is rewritten with standard JSON formatting; only the additions change content")
    if "hooks" not in data:
        created.append("<hooks>")
    return Plan(runtime, target, "install", text, _dump(after, text), notes, created)


def _with_command(group: Any, command: str) -> Any:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return group
    return {**group, "hooks": [{**h, "command": command} if is_relay(h) else h for h in group["hooks"]]}


def _with_ask(groups: list[Any], command: str) -> tuple[list[Any], bool]:
    """Robin's question hook in its own group: added if missing; its command, timeout and matcher kept
    current."""
    want = {
        "matcher": ASK_MATCHER,
        "hooks": [{"type": "command", "command": command, "timeout": ASK_TIMEOUT_S}],
    }
    for n, g in enumerate(groups):
        if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(is_ask(h) for h in g["hooks"]):
            hooks = [
                {**h, "command": command, "timeout": ASK_TIMEOUT_S} if is_ask(h) else h for h in g["hooks"]
            ]
            current = {**g, "matcher": ASK_MATCHER, "hooks": hooks}
            if current == g:
                return groups, False
            return groups[:n] + [current] + groups[n + 1 :], True
    return groups + [want], True


def plan_uninstall(runtime: str, data_dir: Path) -> Plan:
    target = config_path(runtime)
    rec = records(data_dir).get(runtime, {})
    if runtime == "pi":
        if not target.exists():
            return Plan(runtime, target, "nothing", None, None, ["not installed"])
        text = target.read_text()
        if rec.get("sha256") and hashlib.sha256(text.encode()).hexdigest() != rec["sha256"]:
            raise InstallError(
                f"{target} changed since Robin wrote it; remove it yourself if you want it gone"
            )
        if RELAY_MODULE not in text:
            raise InstallError(f"{target} isn't Robin's observer; leaving it alone")
        return Plan(runtime, target, "uninstall", text, None)

    data, before = _load_json(target)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return Plan(runtime, target, "nothing", before, before, ["not installed"])
    created = set(rec.get("created_events", []))
    new_hooks: dict[str, Any] = {}
    changed = False
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept = []
        for g in groups:
            if (
                isinstance(g, dict)
                and isinstance(g.get("hooks"), list)
                and any(is_robin(h) for h in g["hooks"])
            ):
                rest = [h for h in g["hooks"] if not is_robin(h)]
                changed = True
                if rest:
                    kept.append({**g, "hooks": rest})
                continue
            kept.append(g)
        if kept or event not in created:
            new_hooks[event] = kept
    if not changed:
        return Plan(runtime, target, "nothing", before, before, ["not installed"])
    after = dict(data)
    if new_hooks or "<hooks>" not in created:
        after["hooks"] = new_hooks
    else:
        after.pop("hooks", None)
    return Plan(runtime, target, "uninstall", before, _dump(after, before))


# -- applying ----------------------------------------------------------------------------------------------


def apply(plan: Plan, data_dir: Path) -> Path | None:
    """Write a confirmed plan: back up the original, write atomically, record it. Returns the backup."""
    if plan.action == "nothing":
        return None
    current = plan.target.read_text() if plan.target.exists() else None
    if current != plan.before:
        raise InstallError(f"{plan.target} changed while you were reviewing; run the command again")
    backup = None
    if current is not None:
        backups = secure_mkdir(data_dir / "hooks" / "backups")
        backup = backups / f"{plan.runtime}-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}{plan.target.suffix}"
        _atomic_write(backup, current, mode=0o600)
    recs = records(data_dir)
    if plan.action == "install":
        assert plan.after is not None
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        mode = plan.target.stat().st_mode & 0o777 if plan.target.exists() else 0o644
        _atomic_write(plan.target, plan.after, mode=mode)
        recs[plan.runtime] = {
            "config": str(plan.target),
            "command": relay_command(plan.runtime, data_dir) if plan.runtime != "pi" else None,
            "ask_command": ask_command(data_dir) if plan.runtime == "claude" else None,
            "events": EVENTS.get(plan.runtime, []),
            # A reinstall keeps what an earlier install created, so uninstall still removes it.
            "created_events": sorted(
                set(plan.created_events) | set(recs.get(plan.runtime, {}).get("created_events", []))
            ),
            "sha256": hashlib.sha256(plan.after.encode()).hexdigest() if plan.runtime == "pi" else None,
            "installed_at": utc_now().isoformat(),
            "backup": str(backup) if backup else None,
        }
    else:
        if plan.after is None:
            plan.target.unlink()
        else:
            _atomic_write(plan.target, plan.after, mode=plan.target.stat().st_mode & 0o777)
        recs.pop(plan.runtime, None)
    _save_records(data_dir, recs)
    return backup


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- status and drift --------------------------------------------------------------------------------------


def status(runtime: str, data_dir: Path) -> dict[str, Any]:
    """Installed or not, and any drift between Robin's record and the config on disk."""
    target = config_path(runtime)
    rec = records(data_dir).get(runtime)
    drift: list[str] = []
    if runtime == "pi":
        present = target.exists() and RELAY_MODULE in target.read_text()
        if rec and not target.exists():
            drift.append("recorded as installed, but the observer file is gone")
        elif rec and rec.get("sha256") and hashlib.sha256(target.read_bytes()).hexdigest() != rec["sha256"]:
            drift.append("the observer file changed since Robin wrote it")
        elif present and not rec:
            drift.append("Robin's observer file is present but not recorded")
        return {"runtime": runtime, "config": str(target), "installed": bool(rec and present), "drift": drift}
    try:
        data, _ = _load_json(target)
    except InstallError as exc:
        return {"runtime": runtime, "config": str(target), "installed": False, "drift": [str(exc)]}
    raw = data.get("hooks")
    hooks: dict[str, Any] = raw if isinstance(raw, dict) else {}
    found: dict[str, list[str]] = {}
    asks: list[dict[str, Any]] = []
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                if is_relay(h):
                    found.setdefault(event, []).append(str(h.get("command")))
                elif is_ask(h):
                    asks.append({**h, "event": event, "matcher": g.get("matcher")})
    if rec and runtime == "claude":
        if not asks:
            drift.append(
                "Robin's question hook is missing; reinstall to answer terminal questions from Robin"
            )
        elif len(asks) > 1:
            drift.append("Robin's question hook appears more than once")
        else:
            a = asks[0]
            if a["event"] != ASK_EVENT or a["matcher"] != ASK_MATCHER:
                drift.append(
                    f"Robin's question hook isn't on {ASK_EVENT} for {ASK_MATCHER}; reinstall to fix it"
                )
            elif a.get("command") != rec.get("ask_command"):
                drift.append(
                    "Robin's question hook runs a different command (Robin moved?); reinstall to update it"
                )
            elif a.get("timeout") != ASK_TIMEOUT_S:
                drift.append(f"Robin's question hook timeout isn't {ASK_TIMEOUT_S}s; reinstall to restore it")
    if rec:
        missing = [e for e in rec.get("events", []) if e not in found]
        if missing:
            drift.append(f"Robin's hook is missing for {', '.join(missing)}")
        stale = sorted({c for cs in found.values() for c in cs if c != rec.get("command")})
        if stale:
            drift.append("a Robin hook runs a different relay command (Robin moved?); reinstall to update it")
        dupes = [e for e, cs in found.items() if len(cs) > 1]
        if dupes:
            drift.append(f"Robin's hook appears more than once for {', '.join(dupes)}")
    elif found:
        drift.append("Robin hooks are present but not recorded; `robin hooks uninstall` removes them")
    return {
        "runtime": runtime,
        "config": str(target),
        "installed": bool(rec and (found or asks)),
        "drift": drift,
    }
