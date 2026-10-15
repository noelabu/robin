"""The `robin` command (PRD Appendix A, TR-053). Every command except serve and doctor is an API client."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from robin import __version__
from robin.paths import default_data_dir

EXIT_ERROR = 1
EXIT_UNAVAILABLE = 3


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# -- talking to the service ----------------------------------------------------


def endpoint(data_dir: Path) -> str | None:
    try:
        return str(json.loads((data_dir / "service.json").read_text())["url"])
    except (OSError, ValueError, KeyError):
        return None


def client(data_dir: Path) -> httpx.Client:
    url = endpoint(data_dir)
    token_path = data_dir / "cli.token"
    if url is None or not token_path.exists():
        raise CliError("Robin isn't running. Start it with `robin` or `robin serve`.", EXIT_UNAVAILABLE)
    return httpx.Client(
        base_url=url, timeout=30, headers={"authorization": f"Bearer {token_path.read_text().strip()}"}
    )


def call(c: httpx.Client, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    try:
        r = c.request(method, f"/api/v1{path}", json=body)
    except httpx.ConnectError as exc:
        raise CliError("Robin isn't answering. Start it with `robin serve`.", EXIT_UNAVAILABLE) from exc
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        e = data.get("error", {})
        raise CliError(f"{e.get('code', r.status_code)}: {e.get('message', r.text)}")
    return data


def action_id() -> str:
    return f"cli-{uuid.uuid4().hex}"


def wait_action(c: httpx.Client, aid: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        a = call(c, "GET", f"/actions/{aid}")
        if (
            a["state"] in ("acknowledged", "failed", "cancelled", "outcome_unknown")
            or time.monotonic() > deadline
        ):
            return dict(a)
        time.sleep(0.2)


def print_action(a: dict[str, Any]) -> None:
    line = f"action {a['action_id']}: {a['state']}"
    if a.get("error"):
        line += f" ({a['error']['code']}: {a['error']['message']})"
    print(line)


def read_text(text: str | None, file: str | None) -> str:
    if file:
        return Path(file).read_text()
    if text is not None:
        return text
    raise CliError("give --text or --file")


# -- commands ------------------------------------------------------------------


def cmd_default(args: argparse.Namespace) -> None:
    if endpoint(args.data) is not None:
        try:
            cmd_open(args)
            return
        except CliError:
            pass  # a stale service.json: start a new service
    cmd_serve(args)


def cmd_serve(args: argparse.Namespace) -> None:
    from robin.service.main import serve

    serve(args.data, port=args.port, open_browser=not getattr(args, "no_browser", False))


def cmd_open(args: argparse.Namespace) -> None:
    if getattr(args, "session", None):
        return cmd_terminal(args)
    with client(args.data) as c:
        url = call(c, "POST", "/auth/bootstrap")["url"]
    print(f"Opening Robin: {url}")
    webbrowser.open(url)


def cmd_terminal(args: argparse.Namespace) -> None:
    """Hand a session to this terminal (FR-037): Robin releases it, then this process becomes the runtime."""
    with client(args.data) as c:
        session = call(c, "GET", f"/sessions/{args.session}")
        if session.get("control_owner") == "robin":
            aid = action_id()
            body = {"type": "handoff", "action_id": aid, "interrupt_running": args.force}
            try:
                call(c, "POST", f"/sessions/{args.session}/actions", body)
            except CliError as exc:
                if "turn_in_progress" in str(exc):
                    raise CliError(f"{exc}\nRun again with --force to end the running turn.") from exc
                raise
            action = wait_action(c, aid, 30)
            if action["state"] != "acknowledged":
                print_action(action)
                raise CliError("Robin didn't release the session; nothing was opened")
        info = call(c, "GET", f"/sessions/{args.session}/terminal")
    if args.print_only:
        print(" ".join(info["command"]))
        return
    print(
        f"Continuing {session['runtime']} session {args.session} here. "
        f"When you're done, give it back with: robin reclaim {args.session}",
        flush=True,
    )
    if info.get("cwd"):
        os.chdir(info["cwd"])
    os.execvp(info["command"][0], info["command"])


def cmd_reclaim(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        aid = action_id()
        call(c, "POST", f"/sessions/{args.session}/actions", {"type": "reclaim", "action_id": aid})
        print_action(wait_action(c, aid, args.wait) if args.wait else call(c, "GET", f"/actions/{aid}"))


def cmd_run(args: argparse.Namespace) -> None:
    prompt = read_text(args.prompt, args.prompt_file) if (args.prompt or args.prompt_file) else None
    with client(args.data) as c:
        aid = action_id()
        r = call(
            c,
            "POST",
            "/sessions/launch",
            {
                "runtime": args.agent,
                "cwd": str(Path(args.cwd).resolve()),
                "prompt": prompt,
                "action_id": aid,
                "task_id": args.task,
            },
        )
        print(f"session {r['session_id']}")
        for warning in r.get("warnings") or []:
            print(f"warning: {warning}", file=sys.stderr)
        print_action(wait_action(c, aid, args.wait) if args.wait else r["action"])


def cmd_control(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"type": args.control, "action_id": action_id()}
    if args.control == "message":
        body["text"] = read_text(args.text, args.file)
    with client(args.data) as c:
        r = call(c, "POST", f"/sessions/{args.session}/actions", body)
        print_action(wait_action(c, body["action_id"], args.wait) if args.wait else r["action"])


def cmd_project_add(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        p = call(c, "POST", "/projects", {"path": str(Path(args.path).resolve()), "name": args.name})
    git = f" ({p['git']['branch']})" if p.get("git") else ""
    print(f"project {p['project_id']}  {p['name']}{git}  {p['path']}")


def _project_for(c: httpx.Client, path: str) -> str:
    return str(call(c, "POST", "/projects", {"path": str(Path(path).resolve())})["project_id"])


def cmd_task_create(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        pid = _project_for(c, args.project)
        t = call(
            c,
            "POST",
            "/tasks",
            {
                "project_id": pid,
                "title": args.title,
                "objective": args.objective or args.title,
                "files": args.attach or [],
            },
        )
    print(f"task {t['task_id']}  {t['title']}  ({len(t['attachments'])} attachment(s))")


def cmd_task_attach(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        try:
            call(
                c,
                "POST",
                f"/tasks/{args.task}/sessions",
                {"session_id": args.session, "confirm_retained_context": args.confirm},
            )
        except CliError as exc:
            if "confirmation_required" in str(exc):
                raise CliError(f"{exc}\nUse --confirm to continue this session here anyway.") from exc
            raise
    print(f"attached {args.session} to {args.task}")


def cmd_tasks(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        tasks = call(c, "GET", "/tasks")["tasks"]
    if not tasks:
        print("no tasks")
    for t in tasks:
        print(f"{t['task_id']}  {t['status']:<9} {t['title']}")


def cmd_scan(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        c.timeout = httpx.Timeout(None)  # a first scan of a large history can take a while
        reports = call(c, "POST", "/adapters/scan", {"runtimes": args.runtime or None})["runtimes"]
    for r in reports:
        line = f"{r['runtime']:<7} {r['found']} session(s), {r['new_sessions']} new, {r['events']} event(s)"
        if r["quarantined"]:
            line += f", {r['quarantined']} quarantined"
        print(line)
        for e in r["errors"]:
            print(f"        skipped: {e}")


def cmd_attention(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        items = call(c, "GET", "/attention")["items"]
    if not items:
        print("nothing needs attention")
    for i in items:
        detail = i["detail"].get("input") or i["detail"].get("reason") or ""
        print(f"{i['attention_id']}  {i['kind']:<9} {i['state']:<12} {i['title']}  {json.dumps(detail)[:80]}")


def cmd_respond(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        aid = action_id()
        call(
            c,
            "POST",
            f"/attention/{args.attention_id}/responses",
            {"decision": args.decision, "action_id": aid},
        )
        print_action(wait_action(c, aid, args.wait) if args.wait else call(c, "GET", f"/actions/{aid}"))


def cmd_sessions(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        sessions = call(c, "GET", "/sessions")["sessions"]
    if args.json:
        print(json.dumps(sessions, indent=2))
        return
    if not sessions:
        print("no sessions")
    for s in sessions:
        print(
            f"{s['session_id']}  {s['runtime']:<7} {s['runtime_state']:<18} {s['connection_state']:<13} "
            f"{s['execution_cwd'] or '-'}"
        )


def cmd_action(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        print_action(call(c, "GET", f"/actions/{args.action_id}"))


def cmd_supervisor_stop(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        sessions = [
            s["session_id"]
            for s in call(c, "GET", "/sessions")["sessions"]
            if s["mode"] == "managed" and s["connection_state"] == "connected"
        ]
        if sessions and not args.yes:
            print("Stopping the supervisor ends these running sessions:")
            for s in sessions:
                print(f"  {s}")
            if input("Type 'stop' to confirm: ").strip() != "stop":
                raise CliError("not stopped")
        r = call(c, "POST", "/supervisor/shutdown", {"confirm_sessions": sessions})
    print(f"supervisor stopping; ended {len(r['ending'])} session(s)")


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = doctor(args.data)
    width = max(len(name) for name, _, _ in checks)
    for name, status, detail in checks:
        print(f"{status.upper():<5} {name:<{width}}  {detail}")
    if any(status == "fail" for _, status, _ in checks):
        raise CliError("some checks failed")


def doctor(data_dir: Path) -> list[tuple[str, str, str]]:
    """Local checks (TR-071): what Robin needs, what each runtime has, what's running."""
    out: list[tuple[str, str, str]] = []
    out.append(("robin", "ok", __version__))
    ver = sqlite3.sqlite_version_info
    out.append(("sqlite", "ok" if ver >= (3, 35, 0) else "fail", sqlite3.sqlite_version))
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        out.append(("sqlite fts5", "ok", "available"))
    except sqlite3.OperationalError:
        out.append(("sqlite fts5", "fail", "not compiled in; search won't work"))
    if data_dir.exists():
        mode = data_dir.stat().st_mode & 0o777
        out.append(
            (
                "data directory",
                "ok" if os.access(data_dir, os.W_OK) and mode == 0o700 else "warn",
                f"{data_dir} (mode {oct(mode)})",
            )
        )
    else:
        out.append(("data directory", "ok", f"{data_dir} (created on first start)"))
    for name, cmd in (
        ("claude", ["claude", "--version"]),
        ("codex", ["codex", "--version"]),
        ("pi", ["pi", "--version"]),
    ):
        exe = shutil.which(cmd[0])
        if exe is None:
            out.append((f"runtime {name}", "warn", "not installed; Robin can't launch it"))
            continue
        try:
            v = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            out.append((f"runtime {name}", "ok", v[0] if v else exe))
        except (OSError, subprocess.TimeoutExpired):
            out.append((f"runtime {name}", "warn", f"{exe} didn't answer --version"))
    try:
        import importlib.metadata as md

        out.append(("claude agent sdk", "ok", md.version("claude-agent-sdk")))
    except Exception:
        out.append(("claude agent sdk", "fail", "not installed"))
    out.append(
        (
            "tmux",
            "ok" if shutil.which("tmux") else "warn",
            "available"
            if shutil.which("tmux")
            else "optional; without it terminal handoff opens the runtime's own command in your terminal",
        )
    )
    out.append(_supervisor_check(data_dir))
    url = endpoint(data_dir)
    if url is None:
        out.append(("service", "warn", "not running (start with `robin`)"))
    else:
        try:
            with client(data_dir) as c:
                h = call(c, "GET", "/health")
            out.append(
                (
                    "service",
                    "ok",
                    f"{url}, supervisor {'connected' if h['supervisor_connected'] else 'not connected'}",
                )
            )
        except CliError as exc:
            out.append(("service", "warn", f"{url} not answering ({exc})"))
    return out


def _supervisor_check(data_dir: Path) -> tuple[str, str, str]:
    import socket

    from robin.supervisor.protocol import decode, encode
    from robin.supervisor.server import socket_path

    sock = socket_path(data_dir)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(3)
            s.connect(str(sock))
            s.sendall(encode({"type": "probe"}))  # a hello would evict the running service's connection
            hello = decode(s.makefile("rb").readline())
        alive = sum(1 for v in hello.get("sessions", {}).values() if v.get("alive"))
        return (
            "supervisor",
            "ok",
            f"protocol {hello.get('protocol')}, generation {hello.get('generation')}, "
            f"{alive} running session(s)",
        )
    except (OSError, ValueError):
        return ("supervisor", "warn", "not running (the service starts it)")


# -- argument parsing ----------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="robin", description="Run, observe and coordinate coding agents.")
    ap.add_argument(
        "--data", type=Path, default=None, help="data directory (default: app data, or ROBIN_DATA_DIR)"
    )
    ap.add_argument("--version", action="version", version=f"robin {__version__}")
    ap.set_defaults(func=cmd_default, port=7333)
    sub = ap.add_subparsers(dest="command")
    # --data is accepted after the subcommand too (`robin doctor --data DIR`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p = sub.add_parser("serve", parents=[common], help="run the service")
    p.add_argument("--port", type=int, default=7333)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("open", parents=[common], help="open the dashboard, or a session in this terminal")
    p.add_argument(
        "session", nargs="?", help="session to continue in this terminal (hands it off from Robin)"
    )
    p.add_argument("--terminal", action="store_true", help="(default when a session is given)")
    p.add_argument("--force", action="store_true", help="end a running turn to hand off now")
    p.add_argument(
        "--print-only", action="store_true", help="hand off and print the command instead of running it"
    )
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("reclaim", parents=[common], help="take a handed-off session back into Robin")
    p.add_argument("session")
    p.add_argument("--wait", type=float, default=15)
    p.set_defaults(func=cmd_reclaim)

    p = sub.add_parser("run", parents=[common], help="launch a managed session")
    p.add_argument("agent", help="claude, codex or pi")
    p.add_argument("--cwd", default=".", help="project folder (default: current directory)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--prompt")
    g.add_argument("--prompt-file")
    p.add_argument("--task", help="task to bind the new session to")
    p.add_argument("--wait", type=float, default=0, help="seconds to wait for the runtime to accept")
    p.set_defaults(func=cmd_run)

    for control, help_text in (
        ("message", "send an instruction"),
        ("interrupt", "request interruption"),
        ("stop", "end the session's runtime process"),
    ):
        p = sub.add_parser(control, parents=[common], help=help_text)
        p.add_argument("session")
        if control == "message":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--text")
            g.add_argument("--file")
        p.add_argument("--wait", type=float, default=0)
        p.set_defaults(func=cmd_control, control=control)

    p = sub.add_parser("project", parents=[common], help="manage projects")
    psub = p.add_subparsers(dest="project_command", required=True)
    s = psub.add_parser("add", parents=[common], help="register a repository or folder")
    s.add_argument("path")
    s.add_argument("--name")
    s.set_defaults(func=cmd_project_add)

    p = sub.add_parser("task", parents=[common], help="manage shared tasks")
    tsub = p.add_subparsers(dest="task_command", required=True)
    s = tsub.add_parser("create", parents=[common], help="create a task")
    s.add_argument("title")
    s.add_argument("--project", default=".", help="project folder (registered if needed)")
    s.add_argument("--objective")
    s.add_argument("--attach", action="append", help="file to attach (repeatable)")
    s.set_defaults(func=cmd_task_create)
    s = tsub.add_parser("attach", parents=[common], help="attach a session to a task")
    s.add_argument("task")
    s.add_argument("session")
    s.add_argument(
        "--confirm", action="store_true", help="continue a session that holds another task's context"
    )
    s.set_defaults(func=cmd_task_attach)

    sub.add_parser("tasks", parents=[common], help="list tasks").set_defaults(func=cmd_tasks)

    sub.add_parser("attention", parents=[common], help="list questions, approvals and failures").set_defaults(
        func=cmd_attention
    )
    for decision, verb in (("allow", "approve"), ("deny", "deny")):
        p = sub.add_parser(verb, parents=[common], help=f"{verb} a pending request")
        p.add_argument("attention_id")
        p.add_argument("--wait", type=float, default=5)
        p.set_defaults(func=cmd_respond, decision=decision)

    p = sub.add_parser("scan", parents=[common], help="index native session history (read-only)")
    p.add_argument("--runtime", action="append", choices=["claude", "codex", "pi"])
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sessions", parents=[common], help="list sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("action", parents=[common], help="show a control action's state")
    p.add_argument("action_id")
    p.set_defaults(func=cmd_action)

    sub.add_parser("doctor", parents=[common], help="check runtimes, storage and services").set_defaults(
        func=cmd_doctor
    )

    p = sub.add_parser("supervisor", parents=[common], help="manage the session supervisor")
    ssub = p.add_subparsers(dest="supervisor_command", required=True)
    s = ssub.add_parser("stop", parents=[common], help="stop the supervisor (ends its running sessions)")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_supervisor_stop)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    args.data = (args.data or default_data_dir()).expanduser().resolve()
    try:
        args.func(args)
    except CliError as exc:
        print(f"robin: {exc}", file=sys.stderr)
        raise SystemExit(exc.code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
