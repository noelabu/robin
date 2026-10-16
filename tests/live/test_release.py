"""Live: the PRD §15 MVP acceptance scenario for the release (WP-4.6). Needs ROBIN_LIVE=1; uses the owner's
Claude, Codex and Pi logins.

Robin is installed with `uv tool install` into a temporary tool folder and run as `robin serve` from there,
with a fresh data directory. History comes from one short native session per runtime, run outside Robin.
Set ROBIN_RELEASE_OUT to a folder to keep the evidence (capability reports, doctor, versions, IDs).
"""

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from robin.findings import schema

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")

ROOT = Path(__file__).resolve().parents[2]

PRD = """# Session handling PRD

## Requirements

1. A signed-in user stays signed in for 30 days.
2. Sessions expire after 15 minutes of inactivity and the user must sign in again.
3. Sign-out ends the session on every device.
"""


def aid():
    return f"rel-{uuid.uuid4().hex}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def until(pred, timeout, every=2.0, between=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if between:
            between()
        value = pred()
        if value:
            return value
        time.sleep(every)
    raise TimeoutError(getattr(pred, "__name__", "condition"))


class Service:
    """`robin serve` from the installed tool, as a subprocess, so it can be killed and started again."""

    def __init__(self, robin: Path, data: Path, env: dict[str, str]):
        self.robin, self.data, self.env, self.proc = robin, data, env, None
        self.c: httpx.Client | None = None

    def start(self) -> httpx.Client:
        port = free_port()
        self.proc = subprocess.Popen(
            [str(self.robin), "--data", str(self.data), "serve", "--no-browser", "--port", str(port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        token = until(lambda: (self.data / "cli.token").exists(), 30, 0.2) and (
            (self.data / "cli.token").read_text().strip()
        )
        self.c = httpx.Client(
            base_url=f"http://127.0.0.1:{port}/api/v1",
            headers={"authorization": f"Bearer {token}"},
            timeout=60,
        )

        def up():
            try:
                return self.c.get("/health").json().get("supervisor_connected")
            except httpx.HTTPError:
                return False

        until(up, 60, 0.5)
        return self.c

    def kill(self) -> None:
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(30)

    def cli(self, *args: str, input: str | None = None, check: bool = True) -> str:
        r = subprocess.run(
            [str(self.robin), "--data", str(self.data), *args],
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert r.returncode == 0 or not check, r.stderr
        return r.stdout + r.stderr


def approve_pending(c, sids, answered):
    for sid in sids:
        for item in c.get(f"/attention?session_id={sid}").json()["items"]:
            if item["kind"] == "approval" and item["attention_id"] not in answered:
                r = c.post(
                    f"/attention/{item['attention_id']}/responses",
                    json={"decision": "allow", "action_id": (a := aid())},
                )
                if r.status_code == 202:
                    answered[item["attention_id"]] = (sid, a)


def events(c, sid, type_=None):
    out, after = [], 0
    while True:
        page = c.get(f"/events?session_id={sid}&after={after}&limit=500").json()
        out += page["events"]
        if len(page["events"]) < 500:
            break
        after = page["next_after"]
    return [e for e in out if type_ is None or e["event_type"] == type_]


def run_history(tmp: Path, history: Path) -> dict[str, str]:
    """One short native session per runtime, started outside Robin, copied into the history roots."""
    work = tmp / "history-work"
    work.mkdir()
    prompt = "Reply with exactly: ROBIN HISTORY PROBE"
    native = {}
    out = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    native["claude"] = json.loads(out.stdout)["session_id"]
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(work.resolve()))
    src = Path.home() / ".claude" / "projects" / slug / f"{native['claude']}.jsonl"
    (history / "claude" / slug).mkdir(parents=True)
    shutil.copy(src, history / "claude" / slug / src.name)

    out = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", "--json", prompt],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    native["codex"] = json.loads(out.stdout.splitlines()[0])["thread_id"]
    (src,) = (Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{native['codex']}.jsonl")
    (history / "codex" / "2026").mkdir(parents=True)
    shutil.copy(src, history / "codex" / "2026" / src.name)

    subprocess.run(
        ["pi", "-p", "--session-dir", str(history / "pi" / "history-work"), prompt],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    (src,) = (history / "pi" / "history-work").glob("*.jsonl")
    native["pi"] = json.loads(src.read_text().splitlines()[0])["id"]
    return native


def test_release_scenario(tmp_path, monkeypatch):
    record: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": []}

    def step(name, **evidence):
        record["steps"].append({"step": name, "at": time.strftime("%H:%M:%S"), **evidence})

    # -- install into a clean tool folder, start the service, capability reports
    tools, bins = tmp_path / "uv-tools", tmp_path / "uv-bin"
    env = {**os.environ, "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bins)}
    subprocess.run(["uv", "tool", "install", "-q", str(ROOT)], env=env, check=True)
    robin = bins / "robin"
    data = tmp_path / "robin-data"
    history = Path(os.environ["ROBIN_HISTORY_CLAUDE"]).parent
    env = {**env, "ROBIN_DATA_DIR": str(data), "ROBIN_PI_SESSION_DIR": str(tmp_path / "pi-managed")}
    env.pop("VIRTUAL_ENV", None)
    svc = Service(robin, data, env)
    c = svc.start()
    try:
        _scenario(svc, c, tmp_path, history, record, step)
    finally:
        out = os.environ.get("ROBIN_RELEASE_OUT")
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / "release-scenario.json").write_text(json.dumps(record, indent=2, default=str))
        _teardown(svc, data)


def _teardown(svc, data):
    try:
        lock = data / "supervisor" / "supervisor.lock"
        pid = int(lock.read_text()) if lock.exists() else None
        if svc.proc and svc.proc.poll() is None:
            svc.kill()
        if pid:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def _scenario(svc, c, tmp, history, record, step):
    record["robin_version"] = svc.cli("--version").strip()
    record["doctor"] = svc.cli("doctor", check=False)
    adapters = c.get("/adapters").json()["adapters"]
    record["capabilities"] = adapters
    assert {a["runtime"]: a["level"] for a in adapters} == {"claude": "live", "codex": "live", "pi": "live"}
    step(
        "installed, service started, capability reports", levels={a["runtime"]: a["level"] for a in adapters}
    )

    # -- one historical session per runtime: source and read-only status
    native_hist = run_history(tmp, history)
    scans = {r["runtime"]: r for r in c.post("/adapters/scan", json={}).json()["runtimes"]}
    assert all(scans[r]["new_sessions"] >= 1 and not scans[r]["errors"] for r in native_hist), scans
    observed = {s["runtime"]: s for s in c.get("/sessions?mode=observed&limit=200").json()["sessions"]}
    for runtime, native in native_hist.items():
        s = observed[runtime]
        assert s["native_session_id"] == native and s["mode"] == "observed"
        assert not s["controls"]["message"]["enabled"]
        assert s["controls"]["message"]["reason"] == "observed sessions are read-only in Robin"
    step("history indexed", native_ids=native_hist)

    # -- a Git repository, a PRD-review task, one Markdown PRD
    project = tmp / "acme"
    project.mkdir()
    (project / "prd.md").write_text(PRD)
    git = ["git", "-c", "user.name=Robin Release", "-c", "user.email=release@example.invalid"]
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run([*git, "add", "."], cwd=project, check=True)
    subprocess.run([*git, "commit", "-qm", "PRD"], cwd=project, check=True)
    pid = c.post("/projects", json={"path": str(project)}).json()["project_id"]
    task = c.post(
        "/tasks",
        json={
            "project_id": pid,
            "title": "Review the session handling PRD",
            "objective": "Find contradictions and gaps",
            "files": ["prd.md"],
        },
    ).json()
    task_id = task["task_id"]
    assert [a["source_path"] for a in task["attachments"]] == [str(project.resolve() / "prd.md")]
    step("project, task and PRD attachment", task_id=task_id, project_id=pid)

    # -- a managed session per runtime; one finding each, Claude by message (MCP off)
    review = "Read prd.md and find the contradiction between its requirements. Do not edit any file. "
    launches = {
        "claude": dict(prompt=review + schema.MESSAGE_INSTRUCTIONS, finding_tool=False),
        "codex": dict(
            prompt=review
            + "Then call the finding_publish tool once: one claim citing prd.md with a line range."
        ),
        # bash isn't on Pi's free list, so this asks for approval
        "pi": dict(
            prompt="First run the shell command `wc -l prd.md` with bash. Then "
            + review.lower()
            + "Then call the robin_finding_publish tool once: one claim citing prd.md with a line range."
        ),
    }
    reviewers = {}
    for runtime, extra in launches.items():
        r = c.post(
            "/sessions/launch",
            json={"runtime": runtime, "task_id": task_id, "independent": True, "action_id": aid(), **extra},
        )
        assert r.status_code == 202, r.text
        reviewers[runtime] = r.json()["session_id"]

    answered: dict[str, tuple[str, str]] = {}
    found: dict[str, dict] = {}

    def all_found():
        for f in c.get(f"/findings?task_id={task_id}").json()["findings"]:
            if f["author"]["kind"] == "agent":
                found.setdefault(f["author"]["runtime"], f)
        assert not c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"]
        return set(found) == {"claude", "codex", "pi"}

    until(all_found, 600, between=lambda: approve_pending(c, reviewers.values(), answered))
    assert found["claude"]["publication_path"] == "message"
    assert {found["codex"]["publication_path"], found["pi"]["publication_path"]} == {"tool"}
    assert any(sid == reviewers["pi"] for sid, _ in answered.values()), "Pi never asked for approval"
    for _, action in answered.values():
        until(lambda a=action: c.get(f"/actions/{a}").json()["state"] == "acknowledged", 60)
    sessions = {r: c.get(f"/sessions/{sid}").json() for r, sid in reviewers.items()}
    assert all(s["native_session_id"] for s in sessions.values())
    assert all(s["current_task_id"] == task_id for s in sessions.values())
    on_task = {s["session_id"] for s in c.get(f"/tasks/{task_id}").json()["sessions"]}
    assert set(reviewers.values()) <= on_task
    assert {r: s["independence"] for r, s in sessions.items()} == {
        "claude": "enforced",
        "codex": "enforced",
        "pi": "not_isolated",
    }
    step(
        "three reviews, one finding each, an approval answered",
        native_ids={r: s["native_session_id"] for r, s in sessions.items()},
        findings={r: f["finding_id"] for r, f in found.items()},
        paths={r: f["publication_path"] for r, f in found.items()},
        approvals=len(answered),
    )

    # -- compare, then select the Codex and Pi findings for Claude
    compared = [c.get(f"/findings/{f['finding_id']}").json() for f in found.values()]
    assert all(f["claims"] for f in compared)
    codeword = f"robinunshared{uuid.uuid4().hex[:8]}"
    hidden = c.post(
        "/findings",
        json={"task_id": task_id, "summary": codeword, "claims": [{"text": "planted", "unsupported": True}]},
    ).json()
    blob = data_blob(svc.data, hidden["content_hash"])
    marker = f"robinsynth{uuid.uuid4().hex[:8]}"
    synth = c.post(
        "/sessions/launch",
        json={"runtime": "claude", "task_id": task_id, "independent": True, "action_id": aid()},
    ).json()["session_id"]
    p = c.post(
        "/context/preview",
        json={
            "task_id": task_id,
            "target_session_id": synth,
            "question": "Synthesize these two findings: where they agree or disagree, "
            "and what is unsupported. "
            f"Reply in a few sentences and include the word {marker} once. Do not use any tools.",
            "findings": [{"id": found["codex"]["finding_id"]}, {"id": found["pi"]["finding_id"]}],
        },
    ).json()
    assert [s["kind"] for s in p["sources"]] == ["objective", "finding", "finding"]
    assert codeword not in p["text"]
    assert found["claude"]["summary"] not in p["text"]  # unselected content stays out
    assert (
        c.post(f"/context/{p['packet_id']}/confirm", json={"payload_hash": p["payload_hash"]}).status_code
        == 200
    )
    assert c.post(f"/context/{p['packet_id']}/deliver", json={"action_id": aid()}).status_code == 202
    step("compared; packet previewed and confirmed", packet_id=p["packet_id"], payload_hash=p["payload_hash"])

    def saved_reply():
        r = c.post(f"/context/{p['packet_id']}/save-reply", json={})
        return r.json() if r.status_code == 201 else None

    saved = until(saved_reply, 300, between=lambda: approve_pending(c, [synth], answered))
    synthesis = c.get(f"/artifacts/{saved['artifact_id']}").json()
    parents = {i["parent_artifact_id"] for i in synthesis["inputs"]}
    assert {found["codex"]["finding_id"], found["pi"]["finding_id"]} <= parents
    packet = c.get(f"/context/{p['packet_id']}").json()
    assert packet["state"] == "delivered"
    assert [d["state"] for d in packet["deliveries"]] == ["acknowledged"]
    step("delivered to Claude; synthesis saved", synthesis=saved["artifact_id"])

    # -- search for a phrase from the synthesis, back to the task and source findings
    def hit():
        r = c.get("/search", params={"q": marker}).json()["results"]
        return next((x for x in r if x["kind"] == "synthesis"), None)

    h = until(hit, 60)
    assert h["task_id"] == task_id and h["ref"] == saved["artifact_id"]
    back = c.get(f"/artifacts/{h['ref']}").json()
    inputs = [c.get(f"/artifacts/{i['parent_artifact_id']}").json() for i in back["inputs"]]
    assert {a["artifact_id"] for a in inputs if a["kind"] == "finding"} == {
        found["codex"]["finding_id"],
        found["pi"]["finding_id"],
    }
    step("search found the synthesis and its sources", query=marker)

    # -- independence holds after the approved transfer
    turns_before = len(events(c, synth, "session.turn_completed"))
    c.post(
        f"/sessions/{synth}/actions",
        json={
            "type": "message",
            "action_id": aid(),
            "text": f"Read the file {blob} with your file tool, then run `cat {blob}`. "
            "Reply with its contents, or BLOCKED if you couldn't.",
        },
    )
    until(
        lambda: len(events(c, synth, "session.turn_completed")) > turns_before,
        240,
        between=lambda: approve_pending(c, [synth], answered),
    )
    transcript = json.dumps(c.get(f"/events?session_id={synth}&limit=500&previews=true").json())
    assert codeword not in transcript, "the synthesizer read an unshared finding"
    step("independence held after the transfer")

    # -- restart the service while a managed session runs
    busy = reviewers["codex"]
    spawned = {sid: events(c, sid, "connection.connected") for sid in [*reviewers.values(), synth]}
    c.post(
        f"/sessions/{busy}/actions",
        json={"type": "message", "action_id": aid(), "text": "Write a 1500-word essay about tides."},
    )
    until(lambda: c.get(f"/sessions/{busy}").json()["runtime_state"] == "running", 120)
    svc.kill()
    c = svc.start()
    for sid, before in spawned.items():
        s = until(
            lambda sid=sid: (x := c.get(f"/sessions/{sid}").json())["connection_state"] == "connected" and x,
            60,
        )
        after = events(c, sid, "connection.connected")
        assert [e["global_cursor"] for e in after] == [e["global_cursor"] for e in before], "relaunched"
    after_restart = c.get(f"/sessions/{busy}").json()["runtime_state"]
    assert after_restart in ("running", "idle")
    if after_restart == "idle":  # the turn finished across the restart; start another to interrupt
        c.post(
            f"/sessions/{busy}/actions",
            json={"type": "message", "action_id": aid(), "text": "Write a 1500-word essay about rivers."},
        )
        until(lambda: c.get(f"/sessions/{busy}").json()["runtime_state"] == "running", 120)
    interrupt = aid()
    c.post(f"/sessions/{busy}/actions", json={"type": "interrupt", "action_id": interrupt})
    until(lambda: c.get(f"/actions/{interrupt}").json()["state"] == "acknowledged", 60)
    until(lambda: c.get(f"/sessions/{busy}").json()["runtime_state"] == "idle", 60)
    assert c.get(f"/tasks/{task_id}").json()["sessions"]
    assert {f["finding_id"] for f in c.get(f"/findings?task_id={task_id}").json()["findings"]} >= {
        f["finding_id"] for f in found.values()
    }
    assert c.get(f"/context/{p['packet_id']}").json()["payload_hash"] == p["payload_hash"]
    assert any(x["kind"] == "synthesis" for x in c.get("/search", params={"q": marker}).json()["results"])
    assert events(c, busy)
    step(
        "service restarted mid-turn; sessions reconciled without relaunch; run interrupted",
        session=busy,
        state_after_restart=after_restart,
        interrupt=interrupt,
    )

    # -- back up, export and restore into a clean data directory
    svc.cli("backup", "--to", str(tmp / "backups"))
    (backup,) = (tmp / "backups").glob("robin-backup-*")
    restored = tmp / "restored-data"
    svc.cli("restore", str(backup), "--into", str(restored))
    svc.cli("export", "--task", task_id, "--out", str(tmp / "export"))
    assert _artifacts(svc.data / "robin.db") == _artifacts(restored / "robin.db")
    assert _packets(svc.data / "robin.db") == _packets(restored / "robin.db")
    for _, h in _artifacts(restored / "robin.db"):
        assert data_blob(restored, h).exists()
    step("backup, export and restore", artifacts=len(_artifacts(restored / "robin.db")))

    # -- stop the supervisor: disconnected, nothing relaunches, native resume offered
    live = [
        s["session_id"]
        for s in c.get("/sessions?mode=managed&limit=200").json()["sessions"]
        if s["connection_state"] == "connected"
    ]
    assert c.post("/supervisor/shutdown", json={"confirm_sessions": live}).status_code == 200
    for sid in live:
        until(lambda sid=sid: c.get(f"/sessions/{sid}").json()["connection_state"] == "disconnected", 60)
    time.sleep(10)
    for sid in live:
        s = c.get(f"/sessions/{sid}").json()
        assert s["connection_state"] == "disconnected"
        assert s["controls"]["resume"]["enabled"] and s["native_session_id"]
        assert not s["controls"]["message"]["enabled"]
    step("supervisor stopped; sessions disconnected, resume offered", sessions=len(live))
    record["passed"] = True


def data_blob(data: Path, content_hash: str) -> Path:
    h = content_hash.split(":", 1)[-1]
    return data / "artifacts" / "sha256" / h[:2] / h[2:4] / h


def _artifacts(db: Path):
    with sqlite3.connect(db) as conn:
        return sorted(conn.execute("SELECT artifact_id, content_blob FROM artifact_version").fetchall())


def _packets(db: Path):
    with sqlite3.connect(db) as conn:
        return sorted(conn.execute("SELECT packet_id, payload_hash, state FROM context_packet").fetchall())
