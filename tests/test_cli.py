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
