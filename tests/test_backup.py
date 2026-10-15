"""Backup, restore and export (FR-043, TR-073, TR-074)."""

import hashlib
import io
import json

import pytest

from robin import backup
from test_context import preview, publish_user
from test_findings import GOOD, block, launch, say, wait


def populated(c, task_id):
    target = launch(c, task_id)
    say(c, target, block(GOOD))
    wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    f = publish_user(c, task_id)
    p = preview(c, task_id, target, findings=[{"id": f["finding_id"]}]).json()
    c.post(f"/context/{p['packet_id']}/confirm", json={"payload_hash": p["payload_hash"]})
    return f, p


def test_backup_while_running_and_restore_reproduces_everything(api, tmp_path):
    store, c, task_id, _ = api
    f, p = populated(c, task_id)
    out = backup.create(store.data_dir, tmp_path / "backups")  # the service holds the store meanwhile
    names = {x.name for x in out.rglob("*")}
    assert {"robin.db", "manifest.json"} <= names
    assert not names & {"cli.token", "service.json"}  # credentials stay behind
    m = json.loads((out / "manifest.json").read_text())
    assert m["blobs"]["missing"] == [] and m["blobs"]["count"] > 0
    assert hashlib.sha256((out / "robin.db").read_bytes()).hexdigest() == m["database"]["sha256"]

    r = backup.restore(out, tmp_path / "restored")
    assert r["mismatches"] == [] and r["artifacts_checked"] >= 2 and r["packets_checked"] == 1
    restored = backup.ReadOnlyStore(tmp_path / "restored")
    try:
        from robin.findings import store as findings

        original = c.get(f"/findings/{f['finding_id']}").json()
        again = findings.get(restored, f["finding_id"])
        assert again["content_hash"] == original["content_hash"] and again["summary"] == original["summary"]
    finally:
        restored.close()
    assert not (tmp_path / "restored" / "cli.token").exists()


def test_restore_refuses_unsafe_or_damaged_input(api, tmp_path):
    store, c, task_id, _ = api
    populated(c, task_id)
    out = backup.create(store.data_dir, tmp_path / "b")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "something").write_text("x")
    with pytest.raises(backup.BackupError, match="isn't empty"):
        backup.restore(out, occupied)
    blob = next(p for p in (out / "blobs").rglob("*") if p.is_file())
    blob.write_bytes(blob.read_bytes() + b"tampered")
    with pytest.raises(backup.BackupError, match="doesn't match its hash"):
        backup.restore(out, tmp_path / "fresh1")
    assert not (tmp_path / "fresh1").exists() and not list(
        tmp_path.glob(".fresh1.restoring-*")
    )  # nothing left
    (out / "robin.db").write_bytes((out / "robin.db").read_bytes() + b"\0")
    with pytest.raises(backup.BackupError, match="doesn't match its manifest"):
        backup.restore(out, tmp_path / "fresh2")


def test_export_is_readable_relative_and_credential_free(api, tmp_path):
    store, c, task_id, _ = api
    f, p = populated(c, task_id)
    ro = backup.ReadOnlyStore(store.data_dir)
    try:
        r = backup.export(ro, tmp_path / "export", task_id)
    finally:
        ro.close()
    tdir = tmp_path / "export" / "tasks" / task_id
    entry = json.loads((tdir / "task.json").read_text())
    assert r["tasks"] == 1 and r["packets"] == 1
    for a in entry["artifacts"]:
        assert not a["file"].startswith("/")  # relative
        assert hashlib.sha256((tdir / a["file"]).read_bytes()).hexdigest() == a["sha256"]
    assert entry["packets"][0]["payload_hash"] == p["payload_hash"]
    assert all(set(s) == {"runtime", "native_session_id", "mode"} for s in entry["sessions"])  # references
    assert GOOD["summary"] in (tdir / "README.md").read_text()
    exported = "".join(x.read_text(errors="ignore") for x in (tmp_path / "export").rglob("*") if x.is_file())
    assert (store.data_dir / "cli.token").read_text().strip() not in exported


def test_cli_backup_restore_and_repo_export_confirmation(api, tmp_path, capsys, monkeypatch):
    from robin.cli import main

    store, c, task_id, project = api
    populated(c, task_id)
    d = ["--data", str(store.data_dir)]
    main(["backup", "--to", str(tmp_path / "bk"), *d])
    assert "credentials and tokens left out" in capsys.readouterr().out
    out = next((tmp_path / "bk").iterdir())
    main(["restore", str(out), "--into", str(tmp_path / "new")])
    assert "checked against their hashes" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["export", "--task", task_id, "--to-repo", *d])
    printed = capsys.readouterr().out
    assert "visible to everyone who can see the repository" in printed and "nothing exported" in printed
    assert not (project / ".robin").exists()
    main(["export", "--task", task_id, "--to-repo", "--yes", *d])
    assert (project / ".robin" / "tasks" / task_id / "task.json").exists()


def test_a_restore_that_fails_late_leaves_nothing_and_can_be_retried(api, tmp_path, monkeypatch):
    store, c, task_id, _ = api
    populated(c, task_id)
    out = backup.create(store.data_dir, tmp_path / "b")
    target = tmp_path / "new-data"
    monkeypatch.setattr(backup, "verify", lambda s: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        backup.restore(out, target)
    assert not target.exists() and not list(tmp_path.glob(".new-data.restoring-*"))
    monkeypatch.undo()
    assert backup.restore(out, target)["mismatches"] == []  # the retry works
