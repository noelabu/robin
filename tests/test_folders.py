import subprocess

from robin.workspace.folders import branch_of, is_writer, shared_with


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_branch_from_head(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "feature/x")
    assert branch_of(str(repo / "src")) == "feature/x"  # unborn HEAD still names its branch
    git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "c")
    git(repo, "checkout", "-q", "--detach")
    assert branch_of(str(repo)).startswith("detached@")
    assert branch_of(str(tmp_path)) is None
    assert branch_of(None) is None


def test_branch_in_a_linked_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "c")
    git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "wt"))
    assert branch_of(str(tmp_path / "wt")) == "side"


def row(sid, cwd, **kw):
    return {
        "session_id": sid,
        "execution_cwd": cwd,
        "archived": 0,
        "runtime_state": "idle",
        "mode": "managed",
        **kw,
    }


def test_shared_folder_rules():
    sessions = [
        row("a", "/w/app"),
        row("b", "/w/app/web"),  # inside
        row("c", "/w/other"),
        row("d", "/w/app", runtime_state="stopped"),  # ended
        row("e", "/w/app", archived=1),
        row("f", "/w/app", mode="observed", runtime_state="unknown"),  # old history, not writing
        row("g", "/w/app", mode="observed", runtime_state="running"),
        row("h", "/w/application"),  # a sibling with a common prefix is not shared
    ]
    assert shared_with(sessions, "/w/app", exclude="a") == ["b", "g"]
    assert not is_writer(sessions[5])
