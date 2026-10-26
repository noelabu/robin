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
    assert [(f["path"], f["change"]) for f in files] == [
        ("b.txt", "modified"),
        ("c.txt", "added"),
        ("d.txt", "deleted"),
        ("e.txt", "added"),
    ]
    assert not truncated
    by = {f["path"]: f for f in files}
    assert by["c.txt"]["before"] is None and by["d.txt"]["after"] is None
    assert (
        blob(root, by["b.txt"]["before"]) == "b.txt\n"
        and blob(root, by["b.txt"]["after"]) == "changed by the run\n"
    )


def blob(root, oid):
    return subprocess.run(
        ["git", "cat-file", "blob", oid], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def test_a_file_dirty_at_the_start_keeps_its_starting_content(tmp_path):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("edited before the run\n")
    start = gitctx.snapshot(str(root), write=True)
    (root / "a.txt").write_text("edited by the run\n")
    (f,), _ = gitctx.changes(start, gitctx.snapshot(str(root)))
    assert (
        blob(root, f["before"]) == "edited before the run\n"
        and blob(root, f["after"]) == "edited by the run\n"
    )


def test_a_large_file_is_hashed_but_not_kept(tmp_path, monkeypatch):
    root = repo(tmp_path / "r")
    monkeypatch.setattr(gitctx, "KEEP_MAX", 10)
    (root / "a.txt").write_text("more than ten bytes\n")
    start = gitctx.snapshot(str(root), write=True)
    oid = start.dirty["a.txt"]
    assert oid and subprocess.run(["git", "cat-file", "-e", oid], cwd=root).returncode != 0


def test_a_store_robin_cant_write_still_gets_a_snapshot(tmp_path, monkeypatch):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("dirty\n")
    real = gitctx._git
    monkeypatch.setattr(gitctx, "_git", lambda cwd, *a, **kw: None if "-w" in a else real(cwd, *a, **kw))
    start = gitctx.snapshot(str(root), write=True)
    assert start and start.dirty["a.txt"]


def test_a_symlink_is_hashed_as_its_link_never_followed(tmp_path):
    root = repo(tmp_path / "r")
    secret = tmp_path / "outside.txt"
    secret.write_text("outside the repository\n")
    (root / "link").symlink_to(secret)
    start = gitctx.snapshot(str(root), write=True)
    link_blob = blob(root, start.dirty["link"])
    assert link_blob == str(secret)
    target = subprocess.run(
        ["git", "hash-object", str(secret)], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    assert subprocess.run(["git", "cat-file", "-e", target], cwd=root).returncode != 0


def test_a_symlink_whose_target_changed_isnt_a_change(tmp_path):
    root = repo(tmp_path / "r")
    (root / "link").symlink_to("a.txt")  # untracked, so in the start snapshot
    start = gitctx.snapshot(str(root))
    (root / "a.txt").write_text("the link's target changed\n")
    files, _ = gitctx.changes(start, gitctx.snapshot(str(root)))
    assert [f["path"] for f in files] == ["a.txt"]


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
    assert [(f["path"], f["change"]) for f in g["changed_files"]] == [("b.txt", "modified")]
    assert g["shared_folder"] and not g["start_late"]  # the other session was working in the same folder

    diff = c.get(f"/sessions/{sid}/runs/{run['run_id']}/diff", params={"path": "b.txt"}).json()
    assert diff["status"] == "ok" and (diff["added"], diff["removed"]) == (1, 1)
    assert diff["hunks"][0]["lines"] == ["-b.txt", "+changed"]
    for path, code in (("a.txt", "not_changed"), ("../b.txt", "bad_path"), ("/etc/hosts", "bad_path")):
        r = c.get(f"/sessions/{sid}/runs/{run['run_id']}/diff", params={"path": path})
        assert r.json()["error"]["code"] == code
    assert c.get(f"/sessions/{sid}/runs/nope/diff", params={"path": "b.txt"}).status_code == 404


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
