"""Read-only diffs: a run's changed file from its kept blobs, and a session folder's changes against HEAD.

Fake secrets are assembled at run time, as in test_redact.
"""

import random
import string
import subprocess

import pytest

from robin import gitctx, gitdiff
from robin.redact.scanner import load
from test_rungit import GIT, repo

rng = random.Random(11)
ALNUM = string.ascii_letters + string.digits
TOKEN = "gh" + "p_" + "".join(rng.choice(ALNUM) for _ in range(36))
KEY = "\n".join(
    [
        "-----BEGIN " + "RSA PRIVATE " + "KEY-----",
        *("".join(rng.choice(ALNUM) for _ in range(64)) for _ in range(4)),
        "-----END " + "RSA PRIVATE " + "KEY-----",
    ]
)


@pytest.fixture(scope="module")
def rules():
    return load()


def oid(root, text):
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def lines(diff):
    return [line for h in diff["hunks"] for line in h["lines"]]


def test_two_blobs_diff_into_hunks(tmp_path, rules):
    root = repo(tmp_path / "r")
    d = gitdiff.blob_diff(rules, str(root), oid(root, "a\nb\nc\n"), oid(root, "a\nB\nc\nd"))
    assert d["status"] == "ok" and (d["added"], d["removed"]) == (2, 1)
    (h,) = d["hunks"]
    assert (h["old_start"], h["old_lines"], h["new_start"], h["new_lines"]) == (1, 3, 1, 4)
    assert h["lines"] == [" a", "-b", "+B", " c", "+d", "\\ No newline at end of file"]


def test_one_side_shows_the_whole_file(tmp_path, rules):
    root = repo(tmp_path / "r")
    added = gitdiff.blob_diff(rules, str(root), None, oid(root, "x\ny\n"))
    assert lines(added) == ["+x", "+y"] and added["hunks"][0]["new_start"] == 1
    deleted = gitdiff.blob_diff(rules, str(root), oid(root, "x\n"), None)
    assert lines(deleted) == ["-x"] and deleted["removed"] == 1


def test_a_blob_thats_gone_or_binary_or_large_says_so(tmp_path, rules, monkeypatch):
    root = repo(tmp_path / "r")
    assert gitdiff.blob_diff(rules, str(root), "0" * 40, None)["status"] == "unavailable"
    raw = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=root,
            input=b"\0\1\2",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    assert gitdiff.blob_diff(rules, str(root), None, raw)["status"] == "binary"
    assert gitdiff.blob_diff(rules, str(root), oid(root, "a\n"), raw)["status"] == "binary"
    monkeypatch.setattr(gitdiff, "FILE_MAX", 3)
    assert gitdiff.blob_diff(rules, str(root), None, oid(root, "four\n"))["status"] == "too_large"


def test_a_long_diff_is_cut_and_marked(tmp_path, rules, monkeypatch):
    root = repo(tmp_path / "r")
    monkeypatch.setattr(gitdiff, "MAX_LINES", 5)
    d = gitdiff.blob_diff(rules, str(root), None, oid(root, "".join(f"{i}\n" for i in range(20))))
    assert d["truncated"] and len(lines(d)) == 5 and d["added"] == 20


def test_secrets_are_redacted_on_both_sides_and_across_lines(tmp_path, rules):
    root = repo(tmp_path / "r")
    before = f"token = {TOKEN}\nkeep\n"
    after = f"token = env\nkeep\n{KEY}\n"
    d = gitdiff.blob_diff(rules, str(root), oid(root, before), oid(root, after))
    shown = "\n".join(lines(d))
    assert TOKEN not in shown and "[REDACTED:github-pat]" in shown
    assert "PRIVATE" not in shown and shown.count("[REDACTED:private-key]") == 1
    assert len(lines(d)) == 9  # -token, +token, the context line and the key's six lines: none lost


def test_a_secret_on_a_context_line_is_redacted_once(tmp_path, rules):
    root = repo(tmp_path / "r")
    d = gitdiff.blob_diff(rules, str(root), oid(root, f"{TOKEN}\na\n"), oid(root, f"{TOKEN}\nb\n"))
    assert lines(d)[0] == " [REDACTED:github-pat]"


def test_changes_list_the_working_tree_against_head(tmp_path):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("a.txt\nmore\n")
    (root / "d.txt").unlink()
    (root / "new.txt").write_text("1\n2\n3\n")
    (root / "bin.dat").write_bytes(b"\0\1")
    sub = root / "sub"
    sub.mkdir()
    c = gitdiff.worktree(str(sub))
    assert c["state"] == "ok" and c["branch"] == "main" and c["repo_root"] == str(root.resolve())
    assert c["files"] == [
        {"path": "a.txt", "change": "modified", "added": 1, "removed": 0},
        {"path": "bin.dat", "change": "untracked", "added": None, "removed": None, "binary": True},
        {"path": "d.txt", "change": "deleted", "added": 0, "removed": 1},
        {"path": "new.txt", "change": "untracked", "added": 3, "removed": 0},
    ]


def test_changes_say_why_there_are_none(tmp_path, monkeypatch):
    assert gitdiff.worktree(None)["state"] == "no_folder"
    assert gitdiff.worktree(str(tmp_path / "gone"))["state"] == "missing"
    (tmp_path / "plain").mkdir()
    assert gitdiff.worktree(str(tmp_path / "plain"))["state"] == "not_repo"
    root = repo(tmp_path / "r")
    assert gitdiff.worktree(str(root))["files"] == []
    real = gitctx.run
    monkeypatch.setattr(
        gitctx, "run", lambda cwd, *a, **kw: None if a[0] == "status" else real(cwd, *a, **kw)
    )
    assert gitdiff.worktree(str(root))["state"] == "unknown"


def test_a_repository_with_no_commits_or_a_detached_head(tmp_path, rules):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=fresh, check=True)
    (fresh / "x.txt").write_text("x\n")
    subprocess.run([*GIT, "add", "x.txt"], cwd=fresh, check=True)
    c = gitdiff.worktree(str(fresh))
    assert c["head"] is None and c["files"] == [
        {"path": "x.txt", "change": "added", "added": 1, "removed": 0}
    ]
    assert lines(gitdiff.worktree_diff(rules, str(fresh), "x.txt")) == ["+x"]

    root = repo(tmp_path / "r")
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=root, check=True)
    (root / "a.txt").write_text("changed\n")
    c = gitdiff.worktree(str(root))
    assert c["branch"] is None and c["head"]
    assert lines(gitdiff.worktree_diff(rules, str(root), "a.txt")) == ["-a.txt", "+changed"]


def test_a_changed_file_diffs_against_head(tmp_path, rules):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text(f"{TOKEN}\n")
    (root / "new.txt").write_text("new\n")
    d = gitdiff.worktree_diff(rules, str(root), "a.txt")
    assert d["change"] == "modified" and lines(d) == ["-a.txt", "+[REDACTED:github-pat]"]
    assert lines(gitdiff.worktree_diff(rules, str(root), "new.txt")) == ["+new"]


@pytest.mark.parametrize("path", ["b.txt", "../../etc/passwd", "/etc/hosts", ":(glob)**", "*.txt", "sub"])
def test_only_a_listed_path_is_read(tmp_path, rules, path):
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("changed\n")
    with pytest.raises(gitdiff.NotListed):
        gitdiff.worktree_diff(rules, str(root), path)


def test_an_untracked_symlink_shows_its_link_not_its_target(tmp_path, rules):
    root = repo(tmp_path / "r")
    secret = tmp_path / "outside.txt"
    secret.write_text("outside the repository\n")
    (root / "link").symlink_to(secret)
    d = gitdiff.worktree_diff(rules, str(root), "link")
    assert lines(d) == ["+" + str(secret), "\\ No newline at end of file"]
    listed = {f["path"]: f for f in gitdiff.worktree(str(root))["files"]}
    assert listed["link"]["added"] is None


def test_a_session_started_in_a_terminal_gets_its_changes(api, tmp_path):
    from robin.events.ingest import ensure_session

    _, c, _, _ = api
    root = repo(tmp_path / "r")
    (root / "a.txt").write_text("changed\n")
    sid = ensure_session(
        api[0], runtime="claude", mode="observed", native_session_id="n1", execution_cwd=str(root)
    )
    changes = c.get(f"/sessions/{sid}/changes").json()
    assert changes["state"] == "ok" and [f["path"] for f in changes["files"]] == ["a.txt"]
    d = c.get(f"/sessions/{sid}/changes/diff", params={"path": "a.txt"}).json()
    assert d["path"] == "a.txt" and d["status"] == "ok" and d["added"] == 1
    assert (
        c.get(f"/sessions/{sid}/changes/diff", params={"path": "b.txt"}).json()["error"]["code"]
        == "not_changed"
    )
    assert (
        c.get(f"/sessions/{sid}/changes/diff", params={"path": "../x"}).json()["error"]["code"] == "bad_path"
    )
    assert c.get("/sessions/nope/changes").status_code == 404
