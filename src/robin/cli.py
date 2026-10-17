"""The `robin` command (PRD Appendix A, TR-053). Every command except serve and doctor is an API client."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
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
                "independent": args.independent,
                "worktree": args.worktree,
                "skip_permissions": args.skip_permissions,
            },
        )
        print(f"session {r['session_id']}")
        if args.skip_permissions:
            print("permission prompts: skipped")
        if args.worktree:
            (w,) = call(c, "GET", f"/worktrees?session_id={r['session_id']}")["worktrees"]
            print(f"worktree {w['worktree_id']}: {w['path']} on branch {w['branch']}")
        if args.independent:
            s = call(c, "GET", f"/sessions/{r['session_id']}")
            label = {
                "enforced": "enforced (can't read other agents' findings)",
                "not_isolated": f"NOT isolated: {args.agent} has no sandbox to enforce it",
            }
            print(f"independent review: {label.get(s.get('independence'), 'unknown')}")
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
        page = call(c, "GET", "/sessions")
        sessions = page["sessions"]
    if page.get("next_offset") and not args.json:
        print(f"(the {len(sessions)} most recent of {page['total']} sessions)")
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
            for s in call(c, "GET", "/sessions?mode=managed&limit=1000")["sessions"]
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


def cmd_supervisor_restart(args: argparse.Namespace) -> None:
    """Restart the supervisor (to upgrade it): only with no turns running, or after draining them (TR-071)."""
    import time as _time

    with client(args.data) as c:

        def managed() -> list[dict[str, Any]]:
            return [
                s
                for s in call(c, "GET", "/sessions?mode=managed&limit=1000")["sessions"]
                if s["mode"] == "managed" and s["connection_state"] == "connected"
            ]

        busy = [s for s in managed() if s["runtime_state"] in ("running", "waiting_for_input")]
        if busy and not args.drain:
            names = ", ".join(f"{s['runtime']} {s['session_id'][:8]}" for s in busy)
            raise CliError(f"turns are running in {names}; wait for them, or use --drain to wait here")
        deadline = _time.time() + args.timeout
        while busy:
            if _time.time() > deadline:
                raise CliError("turns were still running when --timeout ran out; nothing was restarted")
            print(f"waiting for {len(busy)} running turn(s) to finish…", flush=True)
            _time.sleep(5)
            busy = [s for s in managed() if s["runtime_state"] in ("running", "waiting_for_input")]
        idle = managed()
        if idle and not args.yes:
            print("Restarting the supervisor ends these sessions' hosts; Robin resumes them afterwards:")
            for s in idle:
                print(f"  {s['runtime']} {s['session_id']}")
            if input("Type 'restart' to confirm: ").strip() != "restart":
                raise CliError("not restarted")
        call(c, "POST", "/supervisor/shutdown", {"confirm_sessions": [s["session_id"] for s in idle]})
        deadline = _time.time() + 10
        while call(c, "GET", "/health")["supervisor_connected"] and _time.time() < deadline:
            _time.sleep(
                0.2
            )  # the old one going away first, so its last "connected" isn't mistaken for the new
        deadline = _time.time() + 60
        while not call(c, "GET", "/health")["supervisor_connected"]:  # the service starts the new one
            if _time.time() > deadline:
                raise CliError("the new supervisor didn't come up within a minute; see `robin doctor`")
            _time.sleep(1)
        resumed = 0
        for s in idle:
            if s.get("native_session_id"):
                call(
                    c,
                    "POST",
                    f"/sessions/{s['session_id']}/actions",
                    {"type": "resume", "action_id": action_id()},
                )
                resumed += 1
    print(f"supervisor restarted; resumed {resumed} session(s)")


def cmd_diagnostics(args: argparse.Namespace) -> None:
    """A previewed, redacted diagnostic bundle (TR-083). Nothing is written until you confirm."""
    from robin import diagnostics

    health = None
    if endpoint(args.data) is not None:
        with client(args.data) as c:
            health = call(c, "GET", "/health")
    bundle = diagnostics.collect(args.data, doctor(args.data), health)
    text, rules = diagnostics.preview(args.data, bundle)
    if args.show:
        print(text)
    print(f"diagnostic bundle: {len(text.encode()):,} bytes; sections: {', '.join(bundle)}")
    print("redactions: " + (", ".join(rules) if rules else "none needed"))
    print("It contains no prompts, messages, transcripts, findings, credentials or tokens.")
    out = Path(args.out).expanduser() if args.out else Path.cwd() / "robin-diagnostics.json"
    if not args.yes and (
        not sys.stdin.isatty() or input(f"Write it to {out}? [y/N] ").strip().lower() != "y"
    ):
        print("nothing written (use --show to read it all first)")
        return
    out.write_text(text + "\n")
    print(f"written to {out}")


def cmd_findings(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        q = f"?task_id={args.task}" if args.task else ""
        items = call(c, "GET", f"/findings{q}")["findings"]
        rejected = call(c, "GET", f"/findings/rejections{q}")["rejections"]
    if args.json:
        print(json.dumps({"findings": items, "rejections": rejected}, indent=2))
        return
    if not items:
        print("no findings")
    for f in items:
        a = f["author"]
        who = "user" if a["kind"] == "user" else f"{a['runtime']} {str(a.get('native_session_id') or '')[:8]}"
        flag = f", {f['unsupported_claims']} unsupported" if f["unsupported_claims"] else ""
        print(
            f"{f['finding_id']}  v{f['version']}  {who:<16} {f['claims']} claims{flag}  {f['summary'][:70]}"
        )
    for r in rejected:
        print(f"rejected {r['submission_id']} ({r['path']}): {'; '.join(r['errors'])[:100]}")


def cmd_finding(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        if args.finding_id == "publish":
            if not args.task or not args.file:
                raise CliError("robin finding publish --task TASK --file finding.json")
            payload = json.loads(Path(args.file).read_text())
            f = call(c, "POST", "/findings", {**payload, "task_id": args.task})
            print(f"published {f['finding_id']} v{f['version']}")
            return
        q = f"?version={args.version}" if args.version else ""
        f = call(c, "GET", f"/findings/{args.finding_id}{q}")
    if args.json:
        print(json.dumps(f, indent=2))
        return
    a = f["author"]
    print(f"{f['finding_id']} v{f['version']} of {f['latest_version']}  task {f['task_id']}")
    print(
        f"author: {'user' if a['kind'] == 'user' else a['runtime'] + ' ' + str(a.get('native_session_id'))}"
        f"  via {f['publication_path']}  {f['created_at']}"
    )
    print(f"\n{f['summary']}\n")
    for i, cl in enumerate(f["claims"], 1):
        ev = ", ".join(
            f"{e.get('path') or e.get('artifact_id')}{':' + e['range'] if e.get('range') else ''}"
            for e in cl["evidence"]
        )
        print(f"{i}. {cl['text']}\n   evidence: {ev or 'unsupported'}")
    for title, key in (("Recommendations", "recommendations"), ("Open questions", "questions")):
        if f[key]:
            print(f"\n{title}:")
            for x in f[key]:
                print(f"- {x}")


def _print_packet(p: dict[str, Any]) -> None:
    r = p["recipient"]
    print(f"packet {p['packet_id']}  [{p['state']}]  to {r['runtime']} session {r['session_id']}")
    for s in p["sources"]:
        print(f"  {s['n']}. {s['label']}  ({s['content_hash'][:19]})")
    if p["redactions"]:
        hits = ", ".join(f"{x['rule_id']} in source {x['source']}" for x in p["redactions"])
        print(f"  redacted: {hits}")
    for f in p["flags"]:
        print(f"  note: {f}")
    for x in p.get("excluded", []):
        print(f"  left out: {x['label']} ({x['reason']}); add --allow-loop {x['id']} to include it")
    print(f"  {p['bytes']:,} bytes, ~{p['approx_tokens']:,} tokens (approximate)")
    print(f"  payload {p['payload_hash']}")
    for d in p["deliveries"]:
        print(f"  delivery {d['action_id']}: {d['state']}" + (f" ({d['error']})" if d["error"] else ""))
    if p["invalid_reason"]:
        print(f"  invalidated: {p['invalid_reason']}")


def cmd_context(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        if args.action == "preview":
            if not (args.task and args.to and args.question):
                raise CliError("robin context preview --task T --to SESSION --question TEXT [--findings A,B]")
            body = {
                "task_id": args.task,
                "target_session_id": args.to,
                "question": args.question,
                "findings": [{"id": f} for f in (args.findings or "").split(",") if f],
                "excerpts": [
                    {"session_id": sid, "after": int(a), "through": int(t) if t else None}
                    for sid, a, t in ((x.split(":", 2) + ["", ""])[:3] for x in args.excerpt or [])
                ],
                "allow_loops": getattr(args, "allow_loop", None) or [],
            }
            p = call(c, "POST", "/context/preview", body)
            if args.show:
                print(p["text"])
            _print_packet(p)
            print(f"\nTo send it: robin context deliver {p['packet_id']} --confirm {p['payload_hash']}")
        elif args.action == "show":
            p = call(c, "GET", f"/context/{args.packet}")
            if args.show:
                print(p["text"])
            _print_packet(p)
        elif args.action == "save":
            r = call(c, "POST", f"/context/{args.packet}/save-reply", {"cursor": args.cursor})
            print(
                f"saved synthesis {r['artifact_id']} from {r['author']['runtime']} "
                f"(inputs: {', '.join(i['artifact_id'] for i in r['inputs'])})"
            )
        elif args.action == "deliver":
            if not args.confirm:
                raise CliError("deliver needs --confirm <payload hash> from the preview you reviewed")
            p = call(c, "GET", f"/context/{args.packet}")
            if p["state"] == "previewed":
                call(c, "POST", f"/context/{args.packet}/confirm", {"payload_hash": args.confirm})
            elif p["payload_hash"] != args.confirm:
                raise CliError("that isn't this packet's payload hash")
            aid = action_id()
            call(c, "POST", f"/context/{args.packet}/deliver", {"action_id": aid})
            print_action(wait_action(c, aid, args.wait))
        else:
            for p in call(c, "GET", "/context" + (f"?task_id={args.task}" if args.task else ""))["packets"]:
                _print_packet(p)


def cmd_grant(args: argparse.Namespace) -> None:
    """Let one session read exact finding versions or transcript ranges (TR-068)."""
    with client(args.data) as c:
        if args.revoke:
            call(c, "DELETE", f"/grants/{args.revoke}")
            print(f"revoked {args.revoke}")
            return
        if not args.session:
            raise CliError("robin grant --session S [--findings A,B] [--excerpt SESSION:AFTER:THROUGH]")
        if args.findings or args.excerpt:
            body = {
                "session_id": args.session,
                "artifacts": [{"id": f} for f in (args.findings or "").split(",") if f],
                "excerpts": [
                    {"session_id": sid, "after": int(a), "through": int(t)}
                    for sid, a, t in ((x.split(":", 2) + ["", ""])[:3] for x in args.excerpt or [])
                ],
                "allow_loops": args.allow_loop or [],
            }
            call(c, "POST", "/grants", body)
        result = call(c, "GET", f"/grants?session_id={args.session}")
    for g in result["grants"]:
        what = (
            f"{g['artifact_id']} v{g['version']}"
            if g["kind"] == "artifact"
            else f"{g['excerpt_session_id']} events {g['after_cursor']}–{g['through_cursor']}"
        )
        print(f"{g['grant_id']}  {g['kind']:<8} {what}")
    for r in result["reads"][-10:]:
        print(f"read {r['at']}  {r['tool']}  {r['bytes']} bytes  {r['content_hash'][:19]}")


SYNTHESIS_QUESTION = (
    "Synthesize these findings. Say where they agree, where they disagree and why, and which claims are "
    "unsupported. Don't count repeated claims as independent confirmation. Mark any new claim you add and "
    "give its evidence."
)


def cmd_compare(args: argparse.Namespace) -> None:
    """Findings for a task side by side: claims and their evidence."""
    with client(args.data) as c:
        items = call(c, "GET", f"/findings?task_id={args.task}")["findings"]
        full = [call(c, "GET", f"/findings/{f['finding_id']}") for f in items]
    if not full:
        print("no findings for this task yet")
    for f in full:
        a = f["author"]
        who = "user" if a["kind"] == "user" else f"{a['runtime']} {str(a.get('native_session_id'))[:8]}"
        print(f"== {f['finding_id']} v{f['version']} · {who} ==")
        print(f"   {f['summary']}")
        for i, cl in enumerate(f["claims"], 1):
            ev = ", ".join(
                f"{e.get('path') or e.get('artifact_id')}{':' + e['range'] if e.get('range') else ''}"
                for e in cl["evidence"]
            )
            print(f"   {i}. {cl['text']}  [{ev or 'unsupported'}]")
        print()


def cmd_synthesize(args: argparse.Namespace) -> None:
    """Preview a synthesis request: the chosen findings, sent to one session with a synthesis question."""
    args.action, args.question = "preview", args.question or SYNTHESIS_QUESTION
    args.excerpt, args.packet = None, None
    cmd_context(args)


def cmd_search(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        params = {"q": " ".join(args.query), "limit": str(args.limit), "offset": str(args.offset)}
        for key in ("kind", "task", "session", "runtime"):
            value = getattr(args, key)
            if value:
                params[{"task": "task_id", "session": "session_id"}.get(key, key)] = value
        r = call(c, "GET", "/search?" + urllib.parse.urlencode(params))
    if not r["results"]:
        print("no matches")
    for x in r["results"]:
        where = x["session_id"][:8] if x["session_id"] else (x["task_id"] or "")[:8]
        print(f"{x['at'][:19]}  {x['kind']:<9} {where:<9} {x['title'][:40]:<40}  {x['snippet']}")
    if r["more"]:
        print(f"… more results (--offset {r['next_offset']})")
    if r["pending_events"]:
        print(f"({r['pending_events']} newest events not indexed yet)")


def cmd_watch(args: argparse.Namespace) -> None:
    with client(args.data) as c:
        if args.state == "status":
            on = call(c, "GET", "/settings/watch")["enabled"]
        else:
            on = call(c, "PUT", "/settings/watch", {"enabled": args.state == "on"})["enabled"]
    if on:
        print("watching Claude Code, Codex and Pi history: Robin reads transcripts as they grow")
    else:
        print("not watching history (run `robin scan` to index it once, or `robin watch on`)")


def cmd_runs(args: argparse.Namespace) -> None:
    """A session's latest runs with their Git context (FR-050)."""
    with client(args.data) as c:
        runs = call(c, "GET", f"/sessions/{args.session}/runs?limit={args.limit}")["runs"]
    if not runs:
        print("no runs yet")
    for r in runs:
        state = (r["outcome"] or "ended") if r["ended_at"] else "running"
        print(f"{r['started_at']}  {state}")
        g = r["git"]
        if g is None:
            print("  not in a Git repository")
            continue
        if g["dirty_at_start"] is None:
            start = "start state unknown"
        else:
            start = f"at {(g['base_commit'] or 'no commits yet')[:12]}, {g['dirty_at_start']} dirty at start"
        print(f"  {g['repo_root']}  {g['branch'] or 'detached'} {start}")
        if g["changed_files"] is None:
            print("  changed files: " + ("unknown" if r["ended_at"] else "listed when it ends"))
        else:
            for f in g["changed_files"]:
                print(f"  {f['change']:<9} {f['path']}")
            if not g["changed_files"]:
                print("  no files changed")
        if g["truncated"]:
            print("  (only the first 1,000 files)")
        if g["start_late"] and g["changed_files"] is not None:
            print("  note: start state read late; earlier changes may be missing")
        if g["shared_folder"]:
            print("  note: another session worked in this folder; some changes may be theirs")


def cmd_worktrees(args: argparse.Namespace) -> None:
    """Worktrees Robin made for sessions, and removing one (FR-051). Removal keeps the branch."""
    with client(args.data) as c:
        if args.action == "list":
            q = f"?task_id={args.task}" if args.task else ""
            for w in call(c, "GET", f"/worktrees{q}")["worktrees"]:
                state = f"removed {w['removed_at'][:10]}" if w["removed_at"] else "present"
                print(f"{w['worktree_id']}  {w['branch']:<40} {state}  {w['path']}")
            return
        if not args.worktree:
            raise CliError("robin worktree remove WORKTREE_ID [--discard-changes]")
        s = call(c, "GET", f"/worktrees/{args.worktree}")
        print(f"{s['path']} (branch {s['branch']}, from {s['from_branch'] or 'a detached HEAD'})")
        if s["uncommitted"]:
            print(f"  {len(s['uncommitted'])} uncommitted file(s): {', '.join(s['uncommitted'][:10])}")
        if s["unmerged_commits"]:
            print(f"  {s['unmerged_commits']} commit(s) not on {s['from_branch']}; the branch keeps them")
        if not args.yes and (not sys.stdin.isatty() or input("Remove this worktree? [y/N] ").lower() != "y"):
            print("not removed")
            return
        r = call(c, "POST", f"/worktrees/{args.worktree}/remove", {"discard_changes": args.discard_changes})
    print(f"removed {r['path']}; branch {r['branch_kept']} kept")


def cmd_notify(args: argparse.Namespace) -> None:
    """Opt-in macOS notifications when a session needs input, fails or finishes (FR-033)."""
    with client(args.data) as c:
        if args.state == "status":
            s = call(c, "GET", "/settings/notifications")
        else:
            body: dict[str, Any] = {"enabled": args.state == "on"}
            if args.kinds:
                body["kinds"] = [k.strip() for k in args.kinds.split(",") if k.strip()]
            s = call(c, "PUT", "/settings/notifications", body)
    if s["enabled"]:
        print(f"notifications on for: {', '.join(s['kinds']) or 'nothing'}")
    else:
        print("notifications off")


def _gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB"


def cmd_storage(args: argparse.Namespace) -> None:
    """Copied activity against the cap (TR-081), and an explicit cleanup with a preview first."""
    with client(args.data) as c:
        u = call(c, "GET", "/storage")
        print(f"copied activity {_gb(u['counted'])} of {_gb(u['cap'])} ({u['ratio']:.0%}, {u['state']})")
        print(
            f"  event payloads {_gb(u['events_inline'] + u['events_blobs'])}, "
            f"search text {_gb(u['search_text'])}"
        )
        print(f"  findings, packets and manifests {_gb(u['protected'])} (kept; never counted or pruned)")
        if args.action != "prune":
            return
        body: dict[str, Any] = {"dry_run": True}
        if args.target_gb is not None:
            body["target_gb"] = args.target_gb
        p = call(c, "POST", "/storage/prune", body)
        if not p["events"]:
            print("nothing to prune: already under the target")
            return
        print(
            f"would prune {p['events']:,} oldest events (~{_gb(p['approx_bytes'])}) "
            f"from {p['sessions']} sessions, "
            f"{p['oldest']} to {p['newest']}; they stay listed but their content becomes unavailable"
        )
        if not args.yes:
            if not sys.stdin.isatty() or input("Prune them? [y/N] ").strip().lower() not in ("y", "yes"):
                print("nothing pruned")
                return
        r = call(c, "POST", "/storage/prune", {**body, "dry_run": False})
        print(f"pruned {r['pruned_events']:,} events; now {_gb(r['after']['counted'])}")


def cmd_backup(args: argparse.Namespace) -> None:
    from robin import backup

    dest = Path(args.to).expanduser() if args.to else Path.home() / "robin-backups"
    try:
        out = backup.create(args.data, dest)
    except backup.BackupError as exc:
        raise CliError(str(exc)) from exc
    m = json.loads((out / "manifest.json").read_text())
    print(f"backup written to {out}")
    print(f"  schema {m['schema_revision']}, {m['blobs']['count']} files; credentials and tokens left out")
    if m["blobs"]["missing"]:
        print(
            f"  warning: {len(m['blobs']['missing'])} referenced files were missing or damaged "
            "in the data dir"
        )


def cmd_restore(args: argparse.Namespace) -> None:
    from robin import backup

    try:
        r = backup.restore(Path(args.backup).expanduser(), Path(args.into).expanduser())
    except backup.BackupError as exc:
        raise CliError(str(exc)) from exc
    print(
        f"restored into {args.into}: {r['restored_blobs']} files, "
        f"{r['artifacts_checked']} artifact versions and "
        f"{r['packets_checked']} packets checked against their hashes"
    )
    if r["mismatches"]:
        raise CliError(f"these don't match their hashes: {', '.join(r['mismatches'])}")
    print(f"start it with: robin --data {args.into}")


def cmd_export(args: argparse.Namespace) -> None:
    from robin import backup

    store = backup.ReadOnlyStore(args.data)
    try:
        if args.to_repo:
            if not args.task:
                raise CliError("--to-repo needs --task (it writes into that task's project)")
            from sqlalchemy import select

            from robin.store.models import Project, Task

            with store.reader.connect() as conn:
                path = conn.execute(
                    select(Project.path)
                    .join(Task, Task.project_id == Project.project_id)
                    .where(Task.task_id == args.task)
                ).scalar()
            if not path:
                raise CliError(f"no task {args.task}")
            out = Path(path) / ".robin"
            print(
                f"This writes into your repository at {out}. "
                "Anything you commit there is visible to everyone "
                "who can see the repository, including findings and shared context."
            )
            if not args.yes and (
                not sys.stdin.isatty() or input("Export there? [y/N] ").strip().lower() != "y"
            ):
                print("nothing exported")
                return
        else:
            out = Path(args.out).expanduser() if args.out else Path.cwd() / "robin-export"
        r = backup.export(store, out, args.task)
    except backup.BackupError as exc:
        raise CliError(str(exc)) from exc
    finally:
        store.close()
    print(
        f"exported {r['tasks']} task(s), {r['artifacts']} artifact versions, "
        f"{r['packets']} packets to {r['out']}"
    )


def cmd_hooks(args: argparse.Namespace) -> None:
    """Opt-in, observe-only hooks (TR-076). Shows exactly what changes and writes only on confirmation."""
    from robin.hooks import install as hooks

    runtimes = [args.runtime] if args.runtime else ["claude", "codex", "pi"]
    if args.action == "status":
        for r in runtimes:
            st = hooks.status(r, args.data)
            state = "installed" if st["installed"] else "not installed"
            print(f"{r:<7} {state:<14} {st['config']}")
            for d in st["drift"]:
                print(f"        drift: {d}")
        return
    if not args.runtime:
        raise CliError(f"name a runtime: robin hooks {args.action} claude|codex|pi")
    try:
        plan = (hooks.plan_install if args.action == "install" else hooks.plan_uninstall)(
            args.runtime, args.data
        )
    except hooks.InstallError as exc:
        raise CliError(str(exc)) from exc
    if plan.action == "nothing":
        print(f"{args.runtime}: nothing to do ({'; '.join(plan.notes)})")
        return
    print(plan.diff() or f"{plan.target} will be removed")
    for note in plan.notes:
        print(f"note: {note}")
    if args.action == "install":
        print("Robin's hooks only report events to Robin. They never approve, block or change anything.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError("not confirmed; run again with --yes to apply this change")
        if input(f"{args.action.capitalize()} these hooks? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return
    try:
        backup = hooks.apply(plan, args.data)
    except hooks.InstallError as exc:
        raise CliError(str(exc)) from exc
    print(
        f"{args.action}ed {args.runtime} hooks in {plan.target}" + (f" (backup: {backup})" if backup else "")
    )


def cmd_hook_relay(args: argparse.Namespace) -> None:
    from robin.hooks.relay import main as relay_main

    relay_main(["--runtime", args.runtime, "--data", str(args.data)])


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
    from robin.hooks import install as hooks

    for runtime in ("claude", "codex", "pi"):
        st = hooks.status(runtime, data_dir)
        if st["drift"]:
            out.append((f"hooks {runtime}", "warn", "; ".join(st["drift"])))
        else:
            out.append(
                (f"hooks {runtime}", "ok", "installed" if st["installed"] else "not installed (optional)")
            )
    from robin.redact.scanner import load as load_rules

    try:
        rs = load_rules(data_dir / "redact.toml")
        extra = sum(1 for r in rs.rules if r.source == "custom")
        skipped = f"; {len(rs.skipped)} unusable: {', '.join(rs.skipped)}" if rs.skipped else ""
        detail = f"{rs.version}, {len(rs.rules) - extra} rules + {extra} custom{skipped}"
        out.append(("secret rules", "warn" if rs.skipped else "ok", detail))
    except Exception as exc:
        out.append(("secret rules", "fail", f"couldn't load: {type(exc).__name__}"))
    out.append(_supervisor_check(data_dir))
    out.append(_port_check(data_dir))
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


def _port_check(data_dir: Path) -> tuple[str, str, str]:
    """TR-071: an available loopback port (or Robin's own service already on it)."""
    import socket

    from robin.service.main import DEFAULT_PORT

    if endpoint(data_dir) is not None:
        return ("loopback port", "ok", "in use by this Robin's service")
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", DEFAULT_PORT))
            return ("loopback port", "ok", f"127.0.0.1:{DEFAULT_PORT} is free")
        except OSError:
            return (
                "loopback port",
                "warn",
                f"127.0.0.1:{DEFAULT_PORT} is taken; start with `robin --port N`",
            )


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
        from robin.supervisor.protocol import PROTOCOL_VERSION

        if hello.get("protocol") != PROTOCOL_VERSION:
            return (
                "supervisor",
                "fail",
                f"speaks protocol {hello.get('protocol')}, this Robin needs {PROTOCOL_VERSION}: "
                "restart it with `robin supervisor restart` (after its turns finish)",
            )
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

    p = sub.add_parser("findings", parents=[common], help="list findings (and rejected submissions)")
    p.add_argument("--task")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("finding", parents=[common], help="show a finding, or `finding publish` your own")
    p.add_argument("finding_id", help="a finding ID, or `publish`")
    p.add_argument("--version", type=int)
    p.add_argument("--task", help="publish: the task")
    p.add_argument("--file", help="publish: JSON with summary, claims, recommendations, questions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_finding)

    p = sub.add_parser("context", parents=[common], help="preview and deliver context to a session")
    p.add_argument("action", choices=["preview", "show", "deliver", "save", "list"])
    p.add_argument("packet", nargs="?", help="show/deliver: the packet ID")
    p.add_argument("--task")
    p.add_argument("--to", help="preview: the session to send it to")
    p.add_argument("--question", help="preview: what the recipient should do with it")
    p.add_argument("--findings", help="preview: finding IDs, comma-separated")
    p.add_argument("--excerpt", action="append", help="preview: SESSION:AFTER[:THROUGH] event cursors")
    p.add_argument("--show", action="store_true", help="print the exact payload")
    p.add_argument("--confirm", help="deliver: the payload hash you reviewed")
    p.add_argument("--cursor", type=int, help="save: which reply (default: the latest since delivery)")
    p.add_argument(
        "--allow-loop",
        action="append",
        metavar="ID",
        help="include this source although it is the recipient's own output (repeatable)",
    )
    p.add_argument("--wait", type=float, default=10)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("share", parents=[common], help="preview sharing findings or excerpts with a session")
    for flag, kw in (
        ("--task", {"required": True}),
        ("--to", {"required": True}),
        ("--question", {"required": True}),
        ("--findings", {}),
        ("--excerpt", {"action": "append"}),
    ):
        p.add_argument(flag, **kw)  # type: ignore[arg-type]
    p.add_argument("--show", action="store_true")
    p.add_argument(
        "--allow-loop",
        action="append",
        metavar="ID",
        help="include this source although it is the recipient's own output (repeatable)",
    )
    p.set_defaults(func=cmd_context, action="preview", packet=None, confirm=None, cursor=None, wait=10)

    p = sub.add_parser("compare", parents=[common], help="a task's findings side by side")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("synthesize", parents=[common], help="preview a synthesis of findings for a session")
    p.add_argument("--task", required=True)
    p.add_argument("--to", required=True, help="the session that writes the synthesis")
    p.add_argument("--findings", required=True, help="finding IDs, comma-separated")
    p.add_argument("--question", help="instead of the default synthesis request")
    p.add_argument("--show", action="store_true")
    p.add_argument(
        "--allow-loop",
        action="append",
        metavar="ID",
        help="include this source although it is the recipient's own output (repeatable)",
    )
    p.set_defaults(func=cmd_synthesize, confirm=None, cursor=None, wait=10)

    p = sub.add_parser("grant", parents=[common], help="let a session read shared findings or excerpts")
    p.add_argument("--session", help="the session that may read")
    p.add_argument("--findings", help="finding IDs, comma-separated (pinned to their current version)")
    p.add_argument("--excerpt", action="append", help="SESSION:AFTER:THROUGH event cursors")
    p.add_argument(
        "--allow-loop",
        action="append",
        metavar="ID",
        help="include this source although it is the recipient's own output (repeatable)",
    )
    p.add_argument("--revoke", metavar="GRANT_ID")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser(
        "search", parents=[common], help="search messages, tools, tasks, findings and sessions"
    )
    p.add_argument("query", nargs="+")
    p.add_argument("--kind", choices=["event", "task", "finding", "synthesis", "session"])
    p.add_argument("--task")
    p.add_argument("--session")
    p.add_argument("--runtime")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0, help="skip this many results (for the next page)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("watch", parents=[common], help="follow native session history as it grows (opt-in)")
    p.add_argument("state", choices=["on", "off", "status"])
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("worktree", parents=[common], help="worktrees Robin made for sessions")
    p.add_argument("action", choices=["list", "remove"])
    p.add_argument("worktree", nargs="?", help="remove: the worktree ID")
    p.add_argument("--task")
    p.add_argument("--discard-changes", action="store_true", help="remove even with uncommitted files")
    p.add_argument("--yes", action="store_true", help="don't ask (after reviewing what it would lose)")
    p.set_defaults(func=cmd_worktrees)

    p = sub.add_parser("runs", parents=[common], help="a session's runs with their Git context")
    p.add_argument("session")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("notify", parents=[common], help="notifications when sessions need you (opt-in)")
    p.add_argument("state", choices=["on", "off", "status"])
    p.add_argument("--kinds", help="input, failed, finished (comma-separated; default: all)")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser(
        "storage", parents=[common], help="storage use, and pruning the oldest copied activity"
    )
    p.add_argument("action", nargs="?", choices=["prune"])
    p.add_argument("--target-gb", type=float, help="prune down to this (default: below the cap)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_storage)

    p = sub.add_parser(
        "backup", parents=[common], help="a consistent backup of Robin's data (no credentials)"
    )
    p.add_argument("--to", help="folder to write the backup into (default ~/robin-backups)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", parents=[common], help="restore a backup into a new, empty data directory")
    p.add_argument("backup")
    p.add_argument("--into", required=True, help="the new data directory")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("export", parents=[common], help="tasks, findings and syntheses as JSON and Markdown")
    p.add_argument("--task")
    p.add_argument("--out")
    p.add_argument("--to-repo", action="store_true", help="write into the task's project under .robin/")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("hooks", parents=[common], help="opt-in hooks for sessions started outside Robin")
    p.add_argument("action", choices=["status", "install", "uninstall"])
    p.add_argument("runtime", nargs="?", choices=["claude", "codex", "pi"])
    p.add_argument("--yes", action="store_true", help="apply without asking (after reviewing the diff)")
    p.set_defaults(func=cmd_hooks)

    p = sub.add_parser("hook-relay", parents=[common], help=argparse.SUPPRESS)
    p.add_argument("--runtime", required=True)
    p.set_defaults(func=cmd_hook_relay)

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
    p.add_argument(
        "--independent", action="store_true", help="a reviewer that can't read other agents' findings"
    )
    p.add_argument("--worktree", action="store_true", help="run in a new Git worktree on its own branch")
    p.add_argument(
        "--skip-permissions",
        action="store_true",
        help="run tools without asking (claude, codex); deny rules and the sandbox still apply",
    )
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
    s = ssub.add_parser("restart", parents=[common], help="restart it (to upgrade); resumes idle sessions")
    s.add_argument("--drain", action="store_true", help="wait for running turns to finish first")
    s.add_argument("--timeout", type=float, default=600)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_supervisor_restart)

    p = sub.add_parser("diagnostics", parents=[common], help="a previewed, redacted diagnostic bundle")
    p.add_argument("--show", action="store_true", help="print the whole bundle")
    p.add_argument("--out")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_diagnostics)
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
