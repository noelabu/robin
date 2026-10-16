"""Optional task worktrees (FR-051, TR-070, TR-072)."""

import subprocess
from pathlib import Path

import pytest

from test_findings import aid, wait
from test_rungit import GIT, repo


@pytest.fixture
def wt_root(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    monkeypatch.setenv("ROBIN_WORKTREES", str(root))
    return root


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def launch(c, task_id, **extra):
    r = c.post(
        "/sessions/launch",
        json={"runtime": "fake", "task_id": task_id, "action_id": aid(), "worktree": True, **extra},
    )
    assert r.status_code == 202, r.text
    return r.json()["session_id"]


def stop(c, sid):
    c.post(f"/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    wait(lambda: c.get(f"/sessions/{sid}").json()["connection_state"] == "disconnected")


def test_a_session_runs_in_its_own_worktree_and_branch(api, wt_root):
    _, c, task_id, project = api
    repo(project)
    head = git(project, "rev-parse", "HEAD").strip()
    sid = launch(c, task_id)
    (wt,) = c.get(f"/worktrees?session_id={sid}").json()["worktrees"]
    assert wt["branch"].startswith("robin/review-auth-") and wt["base_commit"] == head
    assert wt["from_branch"] == "main" and wt["path"].startswith(str(wt_root.resolve()))
    assert c.get(f"/sessions/{sid}").json()["execution_cwd"] == wt["path"]
    assert wt["path"] in git(project, "worktree", "list")
    assert git(project, "branch", "--show-current").strip() == "main"  # the checkout is untouched


def test_removal_says_what_it_would_lose_and_keeps_the_branch(api, wt_root):
    store, c, task_id, project = api
    repo(project)
    sid = launch(c, task_id)
    (wt,) = c.get(f"/worktrees?session_id={sid}").json()["worktrees"]
    path = wt["path"]

    r = c.post(f"/worktrees/{wt['worktree_id']}/remove", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_running"
    stop(c, sid)

    Path(path, "work.txt").write_text("done\n")
    subprocess.run([*GIT, "add", "work.txt"], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-qm", "agent work"], cwd=path, check=True)
    Path(path, "scratch.txt").write_text("not committed\n")
    s = c.get(f"/worktrees/{wt['worktree_id']}").json()
    assert s["uncommitted"] == ["scratch.txt"] and s["unmerged_commits"] == 1

    r = c.post(f"/worktrees/{wt['worktree_id']}/remove", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "uncommitted_changes"
    assert r.json()["error"]["detail"]["uncommitted"] == ["scratch.txt"]
    assert Path(path, "scratch.txt").exists()  # nothing removed

    done = c.post(f"/worktrees/{wt['worktree_id']}/remove", json={"discard_changes": True}).json()
    assert done["removed_at"] and not done["exists"] and not Path(path).exists()
    assert git(project, "log", "--format=%s", "-1", wt["branch"]).strip() == "agent work"  # branch kept
    with store.reader.connect() as conn:
        actions = [r[0] for r in conn.exec_driver_sql("SELECT action FROM audit_entry ORDER BY at")]
    assert actions[-2:] == ["worktree.create", "worktree.remove_discarding"]


def test_a_clean_worktree_is_removed_without_discarding(api, wt_root):
    _, c, task_id, project = api
    repo(project)
    sid = launch(c, task_id)
    (wt,) = c.get(f"/worktrees?session_id={sid}").json()["worktrees"]
    stop(c, sid)
    r = c.post(f"/worktrees/{wt['worktree_id']}/remove", json={})
    assert r.status_code == 200 and r.json()["branch_kept"] == wt["branch"]
    assert c.post(f"/worktrees/{wt['worktree_id']}/remove", json={}).status_code == 409


def test_a_worktree_needs_a_repository_with_a_commit(api, wt_root):
    _, c, task_id, project = api
    r = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "action_id": aid(), "worktree": True}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_repository"
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    r = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "action_id": aid(), "worktree": True}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_commits"


def test_cli_runs_in_a_worktree_lists_and_removes_it(api, wt_root, capsys):
    from robin.cli import main
    from robin.service.main import write_endpoint

    store, c, task_id, project = api
    repo(project)
    write_endpoint(store, int(c.base_url.port))
    d = ["--data", str(store.data_dir)]
    main(["run", "fake", "--task", task_id, "--cwd", str(project), "--worktree", *d])
    out = capsys.readouterr().out
    sid = out.split("session ", 1)[1].split()[0]
    assert "on branch robin/review-auth-" in out
    (wt,) = c.get(f"/worktrees?session_id={sid}").json()["worktrees"]
    main(["worktree", "list", *d])
    assert wt["worktree_id"] in capsys.readouterr().out
    stop(c, sid)
    main(["worktree", "remove", wt["worktree_id"], "--yes", *d])
    assert f"branch {wt['branch']} kept" in capsys.readouterr().out


def test_a_moved_file_is_listed_once_by_its_paths(api, wt_root):
    """Regression (R20261016-094531-I1): renames in `git status -z` were cut short, counted twice."""
    _, c, task_id, project = api
    repo(project)
    sid = launch(c, task_id)
    (wt,) = c.get(f"/worktrees?session_id={sid}").json()["worktrees"]
    subprocess.run(["git", "mv", "a.txt", "moved.txt"], cwd=wt["path"], check=True)
    assert c.get(f"/worktrees/{wt['worktree_id']}").json()["uncommitted"] == ["a.txt", "moved.txt"]


def test_a_launch_that_fails_after_the_worktree_leaves_nothing_behind(api, wt_root, monkeypatch):
    """Regression (R20261016-094531-N1): an unrecorded worktree couldn't be listed or removed."""
    from robin.service import app as app_module

    _, c, task_id, project = api
    repo(project)

    def boom(*a, **kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "ensure_session", boom)
    try:
        c.post(
            "/sessions/launch",
            json={"runtime": "fake", "task_id": task_id, "action_id": aid(), "worktree": True},
        )
    except Exception:
        pass  # the test client re-raises the server error
    assert git(project, "worktree", "list").count("\n") == 1  # only the main checkout
    assert "robin/" not in git(project, "branch", "--list")
