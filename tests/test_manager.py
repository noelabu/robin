"""The runtime manager against a real supervisor and the scripted runtime (TR-040–044)."""

import asyncio

import pytest
from sqlalchemy import select

from robin.events.ingest import ensure_session
from robin.runtime.ledger import ActionRejected
from robin.runtime.manager import RuntimeManager
from robin.store.models import Session as SessionRow
from robin.supervisor.client import SupervisorBridge, SupervisorClient, committed_cursor


def session_row(store, sid):
    with store.reader.connect() as c:
        return c.execute(select(SessionRow).where(SessionRow.session_id == sid)).mappings().one()


async def wait_for(pred, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.05)


async def connect(store):
    manager = RuntimeManager(store)
    await manager.recover()
    client = SupervisorClient(store.data_dir)
    await client.connect(after=committed_cursor(store))
    await manager.attach(client)
    bridge = SupervisorBridge(store, client, on_committed=manager.on_committed)
    task = asyncio.create_task(bridge.run())
    return manager, client, task


async def test_launch_follow_up_interrupt_and_state(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)

    launch = await manager.launch(
        sid, runtime="fake", cwd=str(store.data_dir), action_id="launch-1", prompt="go"
    )
    assert launch.state == "submitted"
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "running")
    assert (await manager.get_action("launch-1")).state == "acknowledged"
    assert session_row(store, sid)["connection_state"] == "connected"

    queued = await manager.message(sid, "also this", action_id="msg-1")
    assert queued.state == "submitted"
    intr = await manager.interrupt(sid, action_id="int-1")
    assert intr.state == "submitted"

    async def settled():
        return (await manager.get_action("int-1")).state == "acknowledged"

    for _ in range(100):
        if await settled():
            break
        await asyncio.sleep(0.05)
    assert await settled()
    assert (await manager.get_action("msg-1")).state == "acknowledged"
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "idle")

    await manager.stop(sid, action_id="stop-1")
    await wait_for(lambda: session_row(store, sid)["connection_state"] == "disconnected")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_identical_action_id_is_not_resent(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(sid, runtime="fake", cwd=str(store.data_dir), action_id="launch-1")
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "idle")
    first = await manager.message(sid, "go", action_id="m1")
    again = await manager.message(sid, "go", action_id="m1")
    assert again.request_hash == first.request_hash
    with pytest.raises(Exception, match="different request"):
        await manager.message(sid, "something else", action_id="m1")
    await manager.stop(sid, action_id="stop-1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_preconditions_reject_and_record_failure(supervised):
    store = supervised
    observed = ensure_session(store, runtime="fake", mode="observed")
    unknown = ensure_session(store, runtime="mystery", mode="managed")
    idle = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    with pytest.raises(ActionRejected, match="read-only"):
        await manager.message(observed, "hi", action_id="o1")
    with pytest.raises(ActionRejected, match="observed-only"):
        await manager.launch(unknown, runtime="mystery", cwd="/tmp", action_id="u1")
    with pytest.raises(ActionRejected, match="disconnected"):
        await manager.message(idle, "hi", action_id="d1")
    assert (await manager.get_action("d1")).state == "failed"
    assert (await manager.get_action("d1")).error_code == "not_connected"

    await manager.launch(idle, runtime="fake", cwd=str(store.data_dir), action_id="l1")
    await wait_for(lambda: session_row(store, idle)["connection_state"] == "connected")
    with pytest.raises(ActionRejected, match="lease generation"):
        await manager.message(idle, "hi", action_id="g1", expected_generation=7)
    await manager.stop(idle, action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_service_restart_keeps_session_and_settles_actions(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(sid, runtime="fake", cwd=str(store.data_dir), action_id="launch-1", prompt="go")
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "running")

    client._writer.transport.abort()  # the service dies
    await asyncio.wait_for(task, 5)
    await manager.connection_lost()
    assert session_row(store, sid)["connection_state"] == "stale"

    manager2, client2, task2 = await connect(store)  # a new service process
    assert session_row(store, sid)["connection_state"] == "connected"
    await manager2.message(sid, "after restart", action_id="m2")
    for _ in range(100):
        if (await manager2.get_action("m2")).state == "acknowledged":
            break
        await asyncio.sleep(0.05)
    assert (await manager2.get_action("m2")).state == "acknowledged"
    await manager2.stop(sid, action_id="stop-1")
    await client2.close()
    await asyncio.wait_for(task2, 5)


async def test_supervisor_gone_marks_disconnected_without_relaunch(supervised):
    import os
    import signal

    from conftest import _supervisor_pid
    from robin.supervisor.client import ensure_supervisor

    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(sid, runtime="fake", cwd=str(store.data_dir), action_id="launch-1")
    await wait_for(lambda: session_row(store, sid)["connection_state"] == "connected")
    os.kill(_supervisor_pid(store.data_dir), signal.SIGKILL)
    await asyncio.wait_for(task, 5)
    await ensure_supervisor(store.data_dir)
    manager2, client2, task2 = await connect(store)
    assert session_row(store, sid)["connection_state"] == "disconnected"
    with pytest.raises(ActionRejected, match="disconnected"):
        await manager2.message(sid, "hi", action_id="m9")
    await client2.close()
    await asyncio.wait_for(task2, 5)


class _FakeClient:
    def __init__(self):
        self.closed = asyncio.Event()
        self.events = asyncio.Queue()
        self.acked = []

    async def ack(self, seq):
        self.acked.append(seq)


def _recs(sid):
    import time

    return [
        {"seq": 1, "gen": "g1", "session": sid, "at": time.time(), "event": {"type": "session_started"}},
        {"seq": 2, "gen": "g1", "session": sid, "at": time.time(), "event": {"type": "turn_started"}},
    ]


async def test_projection_catches_up_after_failure_between_ingest_and_projection(store):
    """Regression (review R20261015-143026-I1): events committed but never projected are caught up."""
    sid = ensure_session(store, runtime="fake", mode="managed")

    async def boom(_):
        raise RuntimeError("crash between ingest and projection")

    bridge = SupervisorBridge(store, _FakeClient(), on_committed=boom)
    await bridge.ingest(_recs(sid))  # the bridge survives the callback error
    assert session_row(store, sid)["runtime_state"] == "unknown"

    manager = RuntimeManager(store)  # a restarted service
    await manager.project_pending()
    assert session_row(store, sid)["runtime_state"] == "running"


async def test_ack_arriving_before_the_reply_settles_the_action(store):
    from robin.runtime import ledger

    sid = ensure_session(store, runtime="fake", mode="managed")
    store.writer.call(
        lambda c: ledger.create(
            c, action_id="a1", session_id=sid, action_type="message", payload={"text": "x"}
        )
    )
    assert store.writer.call(lambda c: ledger.transition(c, "a1", "acknowledged")).state == "acknowledged"
    assert store.writer.call(lambda c: ledger.transition(c, "a1", "submitted")).state == "acknowledged"


async def test_launch_fails_when_host_exits_before_session_starts(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(
        sid, runtime="fake", cwd=str(store.data_dir), action_id="l1", env={"ROBIN_FAKE_FAIL_START": "1"}
    )
    for _ in range(100):
        if (await manager.get_action("l1")).state == "failed":
            break
        await asyncio.sleep(0.05)
    action = await manager.get_action("l1")
    assert action.state == "failed" and action.error_code == "host_exited_before_start"
    await client.close()
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("runtime", ["codex", "pi"])
@pytest.mark.parametrize("binary", ["dies", "missing"])
async def test_launch_whose_runtime_binary_fails_shows_as_failed(supervised, tmp_path, runtime, binary):
    """A runtime that dies at once (node missing behind the codex shim) or isn't installed fails the launch
    promptly, with the session failed and a failure item saying why, instead of a silent 60s hang."""
    import os

    from robin.runtime import attention

    store = supervised
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if binary == "dies":
        shim = bin_dir / runtime
        shim.write_text("#!/bin/sh\necho 'env: node: No such file or directory' >&2\nexit 127\n")
        shim.chmod(0o755)
        path = f"{bin_dir}:{os.environ['PATH']}"
    else:
        path = str(bin_dir)  # nothing else on PATH, so an installed runtime can't be found either
    sid = ensure_session(store, runtime=runtime, mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(sid, runtime=runtime, cwd=str(tmp_path), action_id="l1", env={"PATH": path})

    def failure():
        items = [i for i in attention.list_items(store, session_id=sid) if i["kind"] == "failure"]
        return items[0] if items else None

    await wait_for(
        lambda: session_row(store, sid)["connection_state"] == "disconnected" and failure() is not None
    )
    assert (await manager.get_action("l1")).state == "failed"
    assert session_row(store, sid)["runtime_state"] == "failed"
    item = failure()
    assert item is not None and item["state"] == "open"
    reason = str(item["detail"]["reason"])
    if binary == "dies":
        assert "(127)" in reason and "env: node: No such file or directory" in reason, reason
    else:
        assert f"'{runtime}'" in reason, reason
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_launch_whose_host_dies_silently_shows_as_failed(supervised):
    """Even with no host_error from the host, a launch that never started marks the session failed."""
    from robin.runtime import attention

    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    manager, client, task = await connect(store)
    await manager.launch(  # Python itself can't start, so the host never says a word
        sid, runtime="fake", cwd=str(store.data_dir), action_id="l1", env={"PYTHONHOME": "/nonexistent"}
    )
    await wait_for(lambda: session_row(store, sid)["connection_state"] == "disconnected")
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "failed")
    items = [i for i in attention.list_items(store, session_id=sid) if i["kind"] == "failure"]
    assert len(items) == 1 and "exited with code" in str(items[0]["detail"]["reason"])
    await client.close()
    await asyncio.wait_for(task, 5)


def test_adapters_register_into_the_host_main_reads(tmp_path):
    """Regression: hosts launched with -m must see adapter registrations (the double-import bug)."""
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "robin.runtime", "--runtime", "claude", "--cwd", str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    first = json.loads(proc.stdout.splitlines()[0])
    assert first.get("reason") != "unknown runtime 'claude'", first
