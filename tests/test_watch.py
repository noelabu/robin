"""Watching native history (FR-041): only changed files are read, kqueue wakes cycles, polling backs it up."""

import json
import os
import threading
import time
from pathlib import Path

from sqlalchemy import select

from robin import watch
from robin.events.ingest import read_events
from robin.store.models import Session as SessionRow
from test_discovery import claude_store, jl, roots

CLAUDE_SID = "11111111-2222-3333-4444-555555555555"


def observed(store):
    with store.reader.connect() as c:
        return {
            r.native_session_id: r.session_id
            for r in c.execute(
                select(SessionRow.native_session_id, SessionRow.session_id).where(
                    SessionRow.mode == "observed"
                )
            )
        }


def append_user(path: Path, text: str) -> None:
    with open(path, "a") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": CLAUDE_SID,
                    "uuid": f"u-{text}",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n"
        )


def test_cycles_read_only_what_changed(store, tmp_path):
    r = roots(tmp_path, "/Users/me/app")
    w = watch.HistoryWatcher(store, roots=r)
    assert w.cycle().changed_files == 4  # three main transcripts and one Claude subagent transcript
    sessions = observed(store)
    assert len(sessions) == 3
    assert w.cycle().changed_files == 0  # nothing changed: nothing read

    main = r["claude"] / "-Users-me-app" / f"{CLAUDE_SID}.jsonl"
    before = len(read_events(store, session_id=sessions[CLAUDE_SID]))
    append_user(main, "one more thing")
    c = w.cycle()
    assert c.changed_files == 1 and [x.runtime for x in c.reports] == ["claude"]
    assert len(read_events(store, session_id=sessions[CLAUDE_SID])) == before + 1

    jl(
        r["codex"] / "2026" / "10" / "16" / "rollout-2026-10-16T01-00-00-019b-new.jsonl",
        {"type": "session_meta", "payload": {"id": "019b-new", "cwd": "/Users/me/app"}},
    )
    assert w.cycle().changed_files == 1 and "019b-new" in observed(store)  # a new session file


def test_a_changed_subagent_transcript_reingests_its_session(store, tmp_path):
    r = roots(tmp_path, "/Users/me/app")
    w = watch.HistoryWatcher(store, roots={"claude": r["claude"]})
    w.cycle()
    sub = next(r["claude"].rglob("subagents/*.jsonl"))
    with open(sub, "a") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "sub-2",
                    "isSidechain": True,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                }
            )
            + "\n"
        )
    c = w.cycle()
    assert c.changed_files == 1 and c.reports[0].found == 1 and not c.reports[0].errors


def test_kqueue_wakes_on_an_append_polling_backs_it_up_and_stop_is_quick(store, tmp_path):
    r = roots(tmp_path, "/Users/me/app")
    w = watch.HistoryWatcher(store, roots=r)
    w.cycle()
    waker = watch.Waker()
    try:
        waker.watch(w.watched_dirs() + w.hot_files())
        target = w.hot_files()[0]
        threading.Timer(0.3, lambda: target.open("a").write("\n")).start()
        started = time.monotonic()
        assert waker.wait(10) is True and time.monotonic() - started < 3
        waker.watch(w.watched_dirs() + w.hot_files())
        assert waker.wait(1.5) is False  # nothing happened: the poll timeout
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        assert waker.wait(30, stop) is False and time.monotonic() - started < 2
    finally:
        waker.close()


def test_the_service_watches_only_once_turned_on(api, monkeypatch):
    from test_findings import wait

    store, c, _, _ = api
    monkeypatch.setattr(watch, "POLL_S", 1.0)
    root = Path(os.environ["ROBIN_HISTORY_CLAUDE"])  # this test's own history folder (see conftest)
    path = claude_store(root, "/Users/me/app")
    assert c.get("/settings/watch").json() == {"enabled": False}
    time.sleep(1.5)
    assert CLAUDE_SID not in observed(store)  # off: nothing read

    assert c.put("/settings/watch", json={"enabled": True}).json() == {"enabled": True}
    wait(lambda: CLAUDE_SID in observed(store), timeout=15)
    sid = observed(store)[CLAUDE_SID]
    n = len(read_events(store, session_id=sid))
    append_user(path, "while watching")
    wait(lambda: len(read_events(store, session_id=sid)) == n + 1, timeout=15)
    c.put("/settings/watch", json={"enabled": False})


def test_a_file_that_failed_to_ingest_is_retried(store, tmp_path, monkeypatch):
    from robin import discovery

    r = roots(tmp_path, "/Users/me/app")
    w = watch.HistoryWatcher(store, roots={"claude": r["claude"]})
    real = discovery._ingest_session
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk busy")
        return real(*a, **kw)

    monkeypatch.setattr(watch, "_ingest_session", flaky)
    assert w.cycle().reports[0].errors == ["11111111-2222-3333-4444-555555555555.jsonl: OSError"]
    assert CLAUDE_SID not in observed(store)
    assert w.cycle().changed_files == 2 and CLAUDE_SID in observed(store)  # retried without any new growth
