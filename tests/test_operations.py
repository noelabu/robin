"""Operations: health, redacted logs, doctor, diagnostics, supervisor restart (TR-071, TR-083, TR-084)."""

import io
import json
import logging
import time

from robin import logs
from test_context import FAKE_TOKEN, observed_session_with
from test_findings import aid, launch, say, wait


def test_health_reports_each_part(api):
    store, c, task_id, _ = api
    h = c.get("/health").json()
    assert h["ready"] is True and set(h["checks"]) == {
        "service",
        "database",
        "index",
        "supervisor",
        "adapters",
        "coverage",
        "sessions",
    }
    assert h["checks"]["database"]["ok"] and h["checks"]["supervisor"]["ok"]
    assert set(h["checks"]["adapters"]["runtimes"]) == {"claude", "codex", "pi"}
    observed_session_with(store, [("connection.gap", {"reason": "missed"})])
    assert c.get("/health").json()["checks"]["coverage"] == {"ok": False, "sessions_with_gaps_24h": 1}


def test_an_indexing_failure_is_reported_once(api, monkeypatch):
    from robin import search

    store, c, _, _ = api
    logs.setup(store.data_dir)

    def broken(_store):
        raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(search, "catch_up", broken)
    for _ in range(3):
        c.put("/settings/watch", json={"enabled": False})  # any change wakes the indexer
        time.sleep(0.3)
    err = wait(lambda: c.get("/health").json()["checks"]["index"]["error"])
    assert err["code"] == "OperationalError" and err["retries"] >= 2
    lines = (store.data_dir / "logs" / "robin.log").read_text().splitlines()
    assert sum("search indexing failed" in line for line in lines) == 1  # not once per retry


def test_logs_are_json_structured_and_redacted(store):
    path = logs.setup(store.data_dir)
    logging.getLogger("robin.test").warning(
        "deploy failed with %s",
        f"token={FAKE_TOKEN}",
        extra={"session_id": "s1", "stage": "deliver", "code": "E1"},
    )
    for h in logging.getLogger("robin").handlers:
        h.flush()
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["session_id"] == "s1" and line["stage"] == "deliver" and line["code"] == "E1"
    assert FAKE_TOKEN not in path.read_text() and "[REDACTED:github-pat]" in line["message"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_doctor_checks_the_port_and_the_supervisor_protocol(api, monkeypatch):
    from robin import cli
    from robin.service.main import write_endpoint
    from robin.supervisor import protocol

    store, c, _, _ = api
    write_endpoint(store, int(c.base_url.port))
    checks = {name: (status, detail) for name, status, detail in cli.doctor(store.data_dir)}
    assert checks["loopback port"][0] == "ok" and checks["supervisor"][0] == "ok"
    monkeypatch.setattr(protocol, "PROTOCOL_VERSION", 99)
    checks = {name: (status, detail) for name, status, detail in cli.doctor(store.data_dir)}
    assert checks["supervisor"][0] == "fail" and "robin supervisor restart" in checks["supervisor"][1]


def test_diagnostics_preview_first_and_hold_no_secrets(api, capsys, monkeypatch, tmp_path):
    from robin.cli import main
    from robin.service.main import write_endpoint

    store, c, task_id, _ = api
    write_endpoint(store, int(c.base_url.port))
    (store.data_dir / "redact.toml").write_text("[[rules]]\nid = \"acme\"\nregex = '''ACME-[0-9]{6}'''\n")
    logs.setup(store.data_dir)
    logging.getLogger("robin.test").warning("saw %s", FAKE_TOKEN)
    sid = launch(c, task_id)
    say(c, sid, "a private prompt that must not be in diagnostics")
    out = tmp_path / "diag.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["diagnostics", "--out", str(out), "--data", str(store.data_dir)])
    printed = capsys.readouterr().out
    assert "nothing written" in printed and not out.exists()
    main(["diagnostics", "--out", str(out), "--yes", "--data", str(store.data_dir)])
    text = out.read_text()
    bundle = json.loads(text)
    assert FAKE_TOKEN not in text and "private prompt" not in text
    assert (store.data_dir / "cli.token").read_text().strip() not in text
    assert bundle["settings"]["redact.toml"] == {"custom_rule_ids": ["acme"]} and "ACME-" not in text
    assert bundle["counts"]["sessions"] and bundle["health"]["checks"]


def test_supervisor_restart_refuses_running_turns_then_resumes_idle_sessions(api, capsys):
    from robin.cli import CliError, main
    from robin.service.main import write_endpoint

    store, c, task_id, _ = api
    write_endpoint(store, int(c.base_url.port))
    d = ["--data", str(store.data_dir)]
    busy = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "prompt": "work", "action_id": aid()}
    ).json()["session_id"]
    wait(lambda: c.get(f"/sessions/{busy}").json()["runtime_state"] in ("running", "waiting_for_input"))
    try:
        main(["supervisor", "restart", "--yes", *d])
        raise AssertionError("restarted with a running turn")
    except (CliError, SystemExit):
        pass
    c.post(f"/sessions/{busy}/actions", json={"type": "interrupt", "action_id": aid()})
    wait(lambda: c.get(f"/sessions/{busy}").json()["runtime_state"] == "idle")
    native = c.get(f"/sessions/{busy}").json()["native_session_id"]

    main(["supervisor", "restart", "--yes", *d])
    assert "resumed 1 session(s)" in capsys.readouterr().out
    s = wait(lambda: (x := c.get(f"/sessions/{busy}").json())["connection_state"] == "connected" and x)
    assert s["native_session_id"] == native  # the same native session, no new turn started
    assert s["runtime_state"] == "idle"


def test_a_rejected_resume_keeps_the_live_sessions_credential(api):
    from robin import retrieval

    store, c, task_id, _ = api
    sid = launch(c, task_id)
    token = retrieval.mint(store, sid)  # what the running host holds
    r = c.post(f"/sessions/{sid}/actions", json={"type": "resume", "action_id": aid()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_running"
    assert retrieval.session_for(store, token) == sid  # still valid
