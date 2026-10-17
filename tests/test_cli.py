"""The robin CLI against the real stack (service, supervisor, scripted runtime)."""

import asyncio
import threading
import time

import pytest

from robin.cli import main
from robin.service.main import build, free_port, write_endpoint


@pytest.fixture
def served(supervised):
    store = supervised
    port = free_port()
    server, _ = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    write_endpoint(store, port)
    project = store.data_dir.parent / "project"
    project.mkdir()
    yield store, project
    server.should_exit = True
    thread.join(10)


def run(capsys, *argv):
    main(list(argv))
    return capsys.readouterr().out


def session_id(out):
    return next(line.split()[1] for line in out.splitlines() if line.startswith("session "))


def test_run_message_interrupt_stop(served, capsys):
    store, project = served
    d = str(store.data_dir)
    out = run(capsys, "run", "fake", "--cwd", str(project), "--prompt", "go", "--wait", "10", "--data", d)
    sid = session_id(out)
    assert "acknowledged" in out
    assert sid in run(capsys, "sessions", "--data", d)
    assert "acknowledged" in run(capsys, "message", sid, "--text", "more", "--wait", "10", "--data", d)
    assert "acknowledged" in run(capsys, "interrupt", sid, "--wait", "10", "--data", d)
    out = run(capsys, "stop", sid, "--wait", "5", "--data", d)
    assert "action cli-" in out


def test_errors_are_reported_with_codes(served, capsys):
    store, _ = served
    with pytest.raises(SystemExit) as exc:
        main(["interrupt", "no-such-session", "--data", str(store.data_dir)])
    assert exc.value.code == 1
    assert "unknown_session" in capsys.readouterr().err


def test_supervisor_stop_names_the_sessions(served, capsys, monkeypatch):
    store, project = served
    d = str(store.data_dir)
    sid = session_id(run(capsys, "run", "fake", "--cwd", str(project), "--wait", "10", "--data", d))
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(SystemExit):
        main(["supervisor", "stop", "--data", d])
    out = capsys.readouterr().out
    assert sid in out  # it listed what would end before asking
    assert "ended 1 session(s)" in run(capsys, "supervisor", "stop", "--yes", "--data", d)


def test_not_running_is_a_clear_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["sessions", "--data", str(tmp_path)])
    assert exc.value.code == 3 and "isn't running" in capsys.readouterr().err


def test_doctor_checks_the_essentials(served, capsys):
    store, _ = served
    out = run(capsys, "doctor", "--data", str(store.data_dir))
    for name in ("sqlite fts5", "data directory", "supervisor", "service", "claude agent sdk"):
        assert name in out
    assert "supervisor connected" in out


async def test_doctor_probe_does_not_evict_the_service_connection(supervised):
    """Regression (R20261015-150405-I1): doctor's supervisor check must not replace the service's link."""
    from robin.cli import _supervisor_check
    from robin.supervisor.client import SupervisorClient

    service = SupervisorClient(supervised.data_dir)
    await service.connect(after=0)
    name, status, detail = await asyncio.to_thread(_supervisor_check, supervised.data_dir)
    assert status == "ok" and "protocol" in detail
    await asyncio.sleep(0.3)
    assert not service.closed.is_set()  # the service's connection is still the supervisor's writer
    assert (await service.request({"type": "status"}))["ok"]
    await service.close()


def test_projects_and_tasks_from_the_cli(served, capsys):
    store, project = served
    d = str(store.data_dir)
    (project / "PRD.md").write_text("# PRD\n")
    assert "project " in run(capsys, "project", "add", str(project), "--data", d)
    out = run(
        capsys, "task", "create", "PRD review", "--project", str(project), "--attach", "PRD.md", "--data", d
    )
    task_id = out.split()[1]
    assert "1 attachment(s)" in out and task_id in run(capsys, "tasks", "--data", d)
    sid = session_id(
        run(capsys, "run", "fake", "--cwd", str(project), "--task", task_id, "--wait", "10", "--data", d)
    )
    other = run(capsys, "task", "create", "Other", "--project", str(project), "--data", d).split()[1]
    with pytest.raises(SystemExit):
        main(["task", "attach", other, sid, "--data", d])
    assert "--confirm" in capsys.readouterr().err
    assert "attached" in run(capsys, "task", "attach", other, sid, "--confirm", "--data", d)
    run(capsys, "stop", sid, "--data", d)


def test_scan_from_the_cli_reads_only_the_configured_stores(served, capsys, tmp_path, monkeypatch):
    from test_discovery import roots

    store, _ = served
    for name, path in roots(tmp_path, "/Users/me/app").items():
        monkeypatch.setenv(f"ROBIN_HISTORY_{name.upper()}", str(path))
    out = run(capsys, "scan", "--data", str(store.data_dir))
    assert "claude  1 session(s), 1 new, 7 event(s)" in out
    assert "codex   1 session(s), 1 new" in out and "pi      1 session(s), 1 new" in out


def test_attention_and_approve_from_the_cli(served, capsys):
    store, project = served
    d = str(store.data_dir)
    run(capsys, "run", "fake", "--cwd", str(project), "--prompt", "go", "--wait", "10", "--data", d)
    deadline = time.time() + 10
    out = ""
    while "approval" not in out and time.time() < deadline:
        out = run(capsys, "attention", "--data", d)
        time.sleep(0.1)
    attention_id = out.split()[0]
    assert "Bash needs approval" in out and "make test" in out
    assert "acknowledged" in run(capsys, "approve", attention_id, "--data", d)


def test_answer_a_question_from_the_cli(served, capsys):
    store, project = served
    d = str(store.data_dir)
    sid = session_id(
        run(capsys, "run", "fake", "--cwd", str(project), "--prompt", "ask", "--wait", "10", "--data", d)
    )
    deadline = time.time() + 10
    while "question" not in (out := run(capsys, "attention", "--data", d)) and time.time() < deadline:
        time.sleep(0.05)
    assert "Which database?" in out and "(robin answer)" in out
    item = out.split()[0]
    with pytest.raises(SystemExit) as exc:
        main(["answer", item, "--data", d])
    assert exc.value.code == 1
    listed = capsys.readouterr().out
    assert "1. Which database?" in listed and "2. Postgres" in listed and "pick several: 1,3" in listed
    assert "acknowledged" in run(capsys, "answer", item, "2", "1,2", "--data", d)
    from robin.activity import conversation

    deadline = time.time() + 10
    expected = 'Answers: {"Which database?": "Postgres", "Which features?": "Auth, Billing"}'
    while expected not in [m.get("text") for m in conversation(store, sid)] and time.time() < deadline:
        time.sleep(0.05)
    assert expected in [m.get("text") for m in conversation(store, sid)]


def test_chat_opens_on_the_session_and_run_can_open_it(served, capsys, monkeypatch):
    store, project = served
    d = str(store.data_dir)
    opened = []
    monkeypatch.setattr("robin.chat.app.run_chat", lambda data, sid: opened.append(sid))
    out = run(capsys, "run", "fake", "--cwd", str(project), "--chat", "--data", d)
    sid = session_id(out)
    run(capsys, "chat", sid, "--data", d)
    assert opened == [sid, sid]
    with pytest.raises(SystemExit) as exc:
        main(["chat", "no-such-session", "--data", d])
    assert exc.value.code == 1 and opened == [sid, sid]  # refused before the screen opens


async def test_the_chat_answers_a_question_against_the_real_service(served, capsys):
    """The terminal UI end to end: the real client, stream and service, and the scripted question."""
    from textual.widgets import Static

    from robin.chat.api import ChatApi
    from robin.chat.app import ChatApp, ChatInput
    from robin.cli import connection

    store, project = served
    d = str(store.data_dir)
    sid = session_id(await asyncio.to_thread(run, capsys, "run", "fake", "--cwd", str(project), "--data", d))
    url, token = connection(store.data_dir)
    app = ChatApp(ChatApi(url, token, sid), sid)

    async def until(check, timeout=10.0):
        deadline = time.time() + timeout
        while not check() and time.time() < deadline:
            await pilot.pause(0.05)
        assert check()

    async def say(text):
        box = app.query_one("#input", ChatInput)
        box.text = text
        box.focus()
        await pilot.press("enter")

    def panel():
        return str(app.query_one("#pending", Static).content)

    def shown():
        return " ".join(str(getattr(w.content, "markup", w.content)) for w in app.query("#log > Static"))

    async with app.run_test() as pilot:
        await until(lambda: "● live" in str(app.query_one("#status", Static).content))
        await say("ask")
        await until(lambda: "Which database?" in panel())
        await say("2")
        await until(lambda: "Which features?" in panel())
        await say("1,3")
        await until(
            lambda: 'Answers: {"Which database?": "Postgres", "Which features?": "Auth, Search"}' in shown()
        )
        assert shown().count("You  ask") == 1
