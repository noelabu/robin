import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from robin.events.ingest import ensure_session
from robin.runtime import state
from robin.store.models import Run
from robin.workspace import tasks as ws
from robin.workspace.tasks import WorkspaceError

T = datetime(2026, 10, 15, tzinfo=UTC)


@pytest.fixture
def project_dir(store):
    d = store.data_dir.parent / "app"
    d.mkdir()
    (d / "PRD.md").write_text("# PRD\nSessions never expire.\n")
    return d


def test_register_plain_directory_and_git_repo(store, project_dir):
    p = ws.register_project(store, str(project_dir))
    assert p["path"] == str(project_dir.resolve()) and p["git"] is None
    subprocess.run(["git", "init", "-q", "-b", "main", str(project_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(project_dir), "remote", "add", "origin", "git@example.com:me/app.git"], check=True
    )
    again = ws.register_project(store, str(project_dir))
    assert again["project_id"] == p["project_id"]  # idempotent by canonical path
    assert again["git"] == {"remote": "git@example.com:me/app.git", "branch": "main"}


def test_project_cannot_overlap_robin_data(store):
    with pytest.raises(WorkspaceError, match="data directory"):
        ws.register_project(store, str(store.data_dir.parent))


def test_task_snapshots_files_without_touching_originals(store, project_dir):
    p = ws.register_project(store, str(project_dir))
    before = (project_dir / "PRD.md").stat()
    t = ws.create_task(
        store,
        project_id=p["project_id"],
        title="PRD review",
        objective="Review the PRD",
        files=["PRD.md"],
        instructions=["Be strict"],
        expected_outputs=["One finding"],
    )
    (f,) = [a for a in t["attachments"] if a["kind"] == "file"]
    assert f["source_path"] == str((project_dir / "PRD.md").resolve()) and f["content_hash"].startswith(
        "sha256:"
    )
    assert (
        store.blobs.read(f["content_hash"].removeprefix("sha256:")) == (project_dir / "PRD.md").read_bytes()
    )
    after = (project_dir / "PRD.md").stat()
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)
    assert {a["kind"] for a in t["attachments"]} == {"file", "instruction", "expected_output"}
    assert t["status"] == "open" and t["revision"] == 1


def test_attachments_must_stay_inside_the_project(store, project_dir, tmp_path):
    p = ws.register_project(store, str(project_dir))
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    with pytest.raises(WorkspaceError, match="outside the project"):
        ws.create_task(store, project_id=p["project_id"], title="t", objective="o", files=[str(outside)])
    os.symlink(outside, project_dir / "link.txt")
    with pytest.raises(WorkspaceError, match="outside the project"):  # a symlink escape
        ws.create_task(store, project_id=p["project_id"], title="t", objective="o", files=["link.txt"])
    with pytest.raises(WorkspaceError, match="outside the project"):
        ws.create_task(store, project_id=p["project_id"], title="t", objective="o", files=["../secret.txt"])


def test_completing_needs_evidence_and_revisions_are_checked(store, project_dir):
    p = ws.register_project(store, str(project_dir))
    t = ws.create_task(store, project_id=p["project_id"], title="t", objective="o")
    with pytest.raises(WorkspaceError, match="evidence"):
        ws.update_task(store, t["task_id"], expected_revision=1, status="completed")
    done = ws.update_task(
        store,
        t["task_id"],
        expected_revision=1,
        status="completed",
        completion_evidence="All three findings reviewed and merged",
    )
    assert done["status"] == "completed" and done["revision"] == 2
    with pytest.raises(WorkspaceError) as exc:
        ws.update_task(store, t["task_id"], expected_revision=1, archived=True)
    assert exc.value.status == 409 and exc.value.detail == {"revision": 2}
    assert ws.update_task(store, t["task_id"], expected_revision=2, archived=True)["archived"]
    assert ws.list_tasks(store, project_id=p["project_id"]) == []  # archived: hidden, not deleted
    assert len(ws.list_tasks(store, project_id=p["project_id"], include_archived=True)) == 1


def run_task(store, sid):
    with store.reader.connect() as c:
        return [
            r.task_id for r in c.execute(select(Run).where(Run.session_id == sid).order_by(Run.started_at))
        ]


def test_reattaching_needs_confirmation_and_binds_from_the_next_run(store, project_dir):
    p = ws.register_project(store, str(project_dir))
    a = ws.create_task(store, project_id=p["project_id"], title="A", objective="a")["task_id"]
    b = ws.create_task(store, project_id=p["project_id"], title="B", objective="b")["task_id"]
    sid = ensure_session(store, runtime="fake", mode="managed")
    ws.bind_new_session(store, sid, task_id=a, project_id=p["project_id"])
    store.writer.call(lambda c: state.apply(c, sid, 1, T, {"type": "turn_started"}))
    with pytest.raises(WorkspaceError) as exc:
        ws.attach_session(store, b, sid)
    assert exc.value.code == "confirmation_required" and exc.value.detail["runs"] == 1
    t = ws.attach_session(store, b, sid, confirm_retained_context=True)
    assert [s["retained_context_disclosed"] for s in t["sessions"]] == [True]
    # the run in flight keeps task A; the next run is bound to B
    store.writer.call(lambda c: state.apply(c, sid, 2, T, {"type": "turn_completed"}))
    store.writer.call(lambda c: state.apply(c, sid, 3, T, {"type": "turn_started"}))
    assert run_task(store, sid) == [a, b]


def test_archive_session_hides_without_deleting(store):
    sid = ensure_session(store, runtime="fake", mode="observed")
    ws.archive_session(store, sid, True)
    ws.archive_session(store, sid, False)
    with pytest.raises(WorkspaceError):
        ws.archive_session(store, "nope", True)


def test_an_ended_turn_never_completes_the_task(store, project_dir):
    p = ws.register_project(store, str(project_dir))
    t = ws.create_task(store, project_id=p["project_id"], title="A", objective="a")["task_id"]
    sid = ensure_session(store, runtime="fake", mode="managed")
    ws.bind_new_session(store, sid, task_id=t, project_id=p["project_id"])
    store.writer.call(lambda c: state.apply(c, sid, 1, T, {"type": "turn_started"}))
    store.writer.call(lambda c: state.apply(c, sid, 2, T, {"type": "turn_completed"}))
    store.writer.call(lambda c: state.apply(c, sid, 3, T, {"type": "host_exited"}))
    with store.reader.connect() as c:
        (run,) = c.execute(select(Run).where(Run.session_id == sid)).mappings()
    assert run["outcome"] == "completed"  # the turn ended...
    task = ws.get_task(store, t)
    assert task["status"] == "open"  # ...and the task is still the user's to close
    assert [s["runtime_state"] for s in task["sessions"]] == ["stopped"]


def test_attaching_back_to_the_current_task_cancels_a_pending_switch(store, project_dir):
    """Regression (R20261015-151650-I1): the latest choice wins; no stale pending task."""
    p = ws.register_project(store, str(project_dir))
    a = ws.create_task(store, project_id=p["project_id"], title="A", objective="a")["task_id"]
    b = ws.create_task(store, project_id=p["project_id"], title="B", objective="b")["task_id"]
    sid = ensure_session(store, runtime="fake", mode="managed")
    ws.bind_new_session(store, sid, task_id=a, project_id=p["project_id"])
    ws.attach_session(store, b, sid, confirm_retained_context=True)  # pending B
    ws.attach_session(store, a, sid)  # back to A
    store.writer.call(lambda c: state.apply(c, sid, 1, T, {"type": "turn_started"}))
    assert run_task(store, sid) == [a]


def test_a_project_never_overlaps_robins_data_directory(store):
    inside = store.data_dir / "hooks"
    inside.mkdir(exist_ok=True)
    for path in (inside, store.data_dir, store.data_dir.parent):
        with pytest.raises(WorkspaceError, match="data directory"):
            ws.register_project(store, str(path))


def test_a_nul_byte_in_a_file_path_is_refused(store, tmp_path):
    (tmp_path / "proj").mkdir()
    p = ws.register_project(store, str(tmp_path / "proj"))
    with pytest.raises(WorkspaceError) as e:
        ws.read_project_file(store, p["project_id"], "a\x00b")
    assert e.value.code == "invalid_path"
    with pytest.raises(WorkspaceError) as e:
        ws.create_task(store, project_id=p["project_id"], title="t", objective="o", files=["a\x00b"])
    assert e.value.code == "invalid_attachment"
