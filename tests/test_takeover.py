"""Continuing a session started outside Robin: the same session becomes managed (takeover)."""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select, update

from robin.adapters import history
from robin.events.ingest import ensure_session
from robin.events.tailer import JsonlTailer
from robin.hooks import ingest as hook_ingest
from robin.runtime.capabilities import capabilities_for
from robin.runtime.ledger import ActionRejected
from robin.runtime.manager import RuntimeManager, _catch_up
from robin.store.models import Event
from robin.store.models import Session as SessionRow
from test_handoff import settled
from test_manager import connect, session_row, wait_for


def observed(store, native="obs-1", **kw):
    return ensure_session(
        store,
        runtime=kw.pop("runtime", "fake"),
        mode="observed",
        native_session_id=native,
        execution_cwd=kw.pop("cwd", str(store.data_dir.parent)),
    )


def transcript(store, sid, tmp_path, *, age_s, runtime="fake", records=({"type": "x"},)):
    """A transcript Robin has tailed for this session, last written `age_s` seconds ago."""
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    tailer = JsonlTailer(store, path, session_id=sid, runtime=runtime)
    try:
        tailer.poll_once()
    finally:
        tailer.close()
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def events(store, sid):
    with store.reader.connect() as c:
        return c.execute(select(func.count()).select_from(Event).where(Event.session_id == sid)).scalar()


async def test_takeover_continues_the_same_session_in_robin(supervised, tmp_path):
    store = supervised
    sid = observed(store)
    transcript(store, sid, tmp_path, age_s=600)
    before = events(store, sid)
    store.writer.call(  # hooks last saw it working
        lambda c: c.execute(
            update(SessionRow).where(SessionRow.session_id == sid).values(runtime_state="running")
        )
    )
    gen = session_row(store, sid)["lease_generation"]
    manager, client, task = await connect(store)
    await manager.takeover(sid, "keep going", action_id="tk-1")
    await settled(manager, "tk-1")
    row = session_row(store, sid)
    assert row["mode"] == "managed" and row["control_owner"] == "robin"
    assert row["native_session_id"] == "obs-1" and row["lease_generation"] == gen + 1
    await wait_for(lambda: session_row(store, sid)["connection_state"] == "connected")
    assert events(store, sid) > before  # history kept, the host's events added to it
    await wait_for(lambda: session_row(store, sid)["runtime_state"] in ("idle", "waiting_for_input"))
    await manager.stop(sid, action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_takeover_refuses_while_a_terminal_holds_the_session(supervised):
    store = supervised
    sid = observed(store)
    manager, client, task = await connect(store)
    terminal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--resume", "obs-1"])
    try:
        with pytest.raises(ActionRejected, match="close it in your terminal first"):
            await manager.takeover(sid, "hi", action_id="tk-1")
    finally:
        terminal.kill()
        terminal.wait()
    assert session_row(store, sid)["mode"] == "observed"
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_takeover_refuses_a_transcript_written_moments_ago(supervised, tmp_path):
    store = supervised
    sid = observed(store)
    transcript(store, sid, tmp_path, age_s=5)
    manager, client, task = await connect(store)
    with pytest.raises(ActionRejected, match="still running in a terminal"):
        await manager.takeover(sid, "hi", action_id="tk-1")
    assert session_row(store, sid)["mode"] == "observed"
    await client.close()
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"native": None}, "no_native_id"),
        ({"cwd": "/nonexistent/robin-folder"}, "no_cwd"),
        ({"runtime": "mystery"}, "unsupported"),
    ],
)
async def test_takeover_refusals_change_nothing(store, kw, code):
    sid = observed(store, **kw)
    with pytest.raises(ActionRejected) as exc:
        await RuntimeManager(store).takeover(sid, "hi", action_id="tk-1")
    assert exc.value.code == code
    assert session_row(store, sid)["mode"] == "observed"


async def test_takeover_of_a_managed_session_is_refused(store):
    sid = ensure_session(store, runtime="fake", mode="managed", native_session_id="m-1")
    with pytest.raises(ActionRejected) as exc:
        await RuntimeManager(store).takeover(sid, "hi", action_id="tk-1")
    assert exc.value.code == "already_managed"


async def test_without_a_supervisor_the_session_stays_observed(store):
    sid = observed(store)
    action = await RuntimeManager(store).takeover(sid, "hi", action_id="tk-1")
    assert action.state == "failed" and action.error_code == "supervisor_unavailable"
    assert session_row(store, sid)["mode"] == "observed"


async def test_a_host_that_never_starts_gives_the_session_back(supervised, monkeypatch):
    store = supervised
    sid = observed(store)
    manager, client, task = await connect(store)
    real = manager._relaunch_env

    async def failing(session_id, row):
        return {**await real(session_id, row), "ROBIN_FAKE_FAIL_START": "1"}

    monkeypatch.setattr(manager, "_relaunch_env", failing)
    await manager.takeover(sid, "hi", action_id="tk-1")
    await settled(manager, "tk-1", state="failed")
    assert (await manager.get_action("tk-1")).error_code == "host_exited_before_start"
    row = session_row(store, sid)
    assert row["mode"] == "observed" and row["control_owner"] is None
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_after_takeover_hooks_no_longer_record_it(supervised):
    store = supervised
    sid = observed(store)
    manager, client, task = await connect(store)
    await manager.takeover(sid, "hi", action_id="tk-1")
    await settled(manager, "tk-1")
    body = {"relay_id": "r1", "payload": {"session_id": "obs-1", "hook_event_name": "Stop"}}
    assert hook_ingest.record(store, "fake", body)["reason"] == "managed"
    await manager.stop(sid, action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


def test_catch_up_reads_what_the_transcript_gained_since_the_last_scan(store, tmp_path, monkeypatch):
    monkeypatch.setitem(history.HISTORY, "fake", history.HISTORY["claude"])
    sid = observed(store)
    line = {
        "type": "assistant",
        "sessionId": "obs-1",
        "uuid": "a1",
        "timestamp": "2026-10-17T01:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "One."}]},
    }
    path = transcript(store, sid, tmp_path, age_s=600, records=(line,))
    before = events(store, sid)
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    **line,
                    "uuid": "a2",
                    "message": {**line["message"], "content": [{"type": "text", "text": "Two."}]},
                }
            )
            + "\n"
        )
    _catch_up(store, sid, "fake")
    assert events(store, sid) == before + 1


def test_takeover_needs_launch_resume_and_new_turns():
    for runtime in ("claude", "codex", "pi", "fake"):
        assert capabilities_for(runtime).allows("takeover") == (True, None)
    ok, reason = capabilities_for("mystery").allows("takeover")
    assert not ok and "observed-only" in reason
