"""Git context per run (FR-050, TR-075): base commit, working-tree state and the files a run changed."""

import subprocess

from robin import gitctx
from test_findings import aid, wait

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    for name in ("a.txt", "b.txt", "d.txt"):
        (path / name).write_text(f"{name}\n")
    subprocess.run([*GIT, "add", "."], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-qm", "base"], cwd=path, check=True)
    return path


def test_a_run_owns_only_the_changes_it_made(tmp_path):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("edited before the run\n")  # already dirty, left alone by the run
    start = gitctx.snapshot(str(root))
    assert start and start.branch == "main" and start.head and list(start.dirty) == ["a.txt"]

    (root / "b.txt").write_text("changed by the run\n")
    (root / "c.txt").write_text("new\n")
    (root / "d.txt").unlink()
    (root / "e.txt").write_text("committed during the run\n")
    subprocess.run([*GIT, "add", "e.txt"], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-qm", "e"], cwd=root, check=True)
    end = gitctx.snapshot(str(root))

    files, truncated = gitctx.changes(start, end)
    assert files == [
        {"path": "b.txt", "change": "modified"},
        {"path": "c.txt", "change": "added"},
        {"path": "d.txt", "change": "deleted"},
        {"path": "e.txt", "change": "added"},
    ]
    assert not truncated


def test_a_folder_outside_git_has_no_context(tmp_path):
    (tmp_path / "plain").mkdir()
    assert gitctx.snapshot(str(tmp_path / "plain")) is None
    assert gitctx.snapshot(None) is None


def test_runs_record_their_git_context_and_label_a_shared_folder(api):
    _, c, task_id, project = api
    repo(project)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout
    sid = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "prompt": "go", "action_id": aid()}
    ).json()["session_id"]
    wait(lambda: (r := c.get(f"/sessions/{sid}/runs").json()["runs"]) and r[0]["git"])
    other = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "prompt": "go", "action_id": aid()}
    ).json()["session_id"]
    wait(lambda: c.get(f"/sessions/{other}").json()["runtime_state"] in ("running", "waiting_for_input"))
    (project / "b.txt").write_text("changed\n")
    c.post(f"/sessions/{sid}/actions", json={"type": "interrupt", "action_id": aid()})

    (run,) = wait(
        lambda: [r for r in c.get(f"/sessions/{sid}/runs").json()["runs"] if r["git"]["end_commit"]]
    )
    g = run["git"]
    assert g["base_commit"] == head.strip() and g["branch"] == "main" and g["dirty_at_start"] == 0
    assert g["changed_files"] == [{"path": "b.txt", "change": "modified"}]
    assert g["shared_folder"] and not g["start_late"]  # the other session was working in the same folder


def test_a_session_outside_git_has_runs_without_git_context(api, tmp_path):
    _, c, _, _ = api
    plain = tmp_path / "plain"
    plain.mkdir()
    sid = c.post(
        "/sessions/launch", json={"runtime": "fake", "cwd": str(plain), "prompt": "go", "action_id": aid()}
    ).json()["session_id"]
    (run,) = wait(lambda: c.get(f"/sessions/{sid}/runs").json()["runs"])
    assert run["git"] is None


def test_a_failed_git_call_is_unknown_not_clean(tmp_path, monkeypatch):
    root = repo(tmp_path / "r")
    real = gitctx._git
    monkeypatch.setattr(
        gitctx, "_git", lambda cwd, *a, **kw: None if a[0] == "status" else real(cwd, *a, **kw)
    )
    try:
        gitctx.snapshot(str(root))
    except gitctx.GitFailed as exc:
        assert exc.root == str(root.resolve())
    else:
        raise AssertionError("an unreadable status read as a clean tree")
