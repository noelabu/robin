"""Live: a real Claude Code session through the full product stack. Runs only with ROBIN_LIVE=1.

Uses the owner's own `claude` login (subscription). Launch, approval, follow-up, interrupt, service loss and
reconnect, stop; checks state projection and the action ledger along the way.
"""

import asyncio
import os

import pytest
from sqlalchemy import select

from robin.events.ingest import ensure_session, read_events
from robin.runtime.manager import RuntimeManager
from robin.store.models import Session as SessionRow
from robin.supervisor.client import SupervisorBridge, SupervisorClient, committed_cursor

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")


def row(store, sid):
    with store.reader.connect() as c:
        return c.execute(select(SessionRow).where(SessionRow.session_id == sid)).mappings().one()


async def until(pred, timeout=180.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = pred()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.25)


async def connect(store):
    manager = RuntimeManager(store)
    await manager.recover()
    client = SupervisorClient(store.data_dir)
    await client.connect(after=committed_cursor(store))
    await manager.attach(client)
    task = asyncio.create_task(SupervisorBridge(store, client, on_committed=manager.on_committed).run())
    return manager, client, task


def pending_approval(store, sid):
    from robin.events.ingest import read_payload_json

    for e in read_events(store, session_id=sid, limit=10_000):
        if e.event_type == "attention.approval_requested":
            return read_payload_json(store, e.global_cursor)["request_id"]
    return None


async def test_claude_end_to_end(supervised, tmp_path):
    store = supervised
    work = tmp_path / "work"
    work.mkdir()
    sid = ensure_session(store, runtime="claude", mode="managed", execution_cwd=str(work))
    manager, client, task = await connect(store)
    await manager.launch(
        sid,
        runtime="claude",
        cwd=str(work),
        action_id="l1",
        prompt="Create a file named probe.txt containing the word hello. Then reply DONE.",
    )
    request_id = await until(lambda: pending_approval(store, sid))
    assert row(store, sid)["runtime_state"] == "waiting_for_input"
    assert row(store, sid)["native_session_id"]  # captured from the SDK init message

    client._writer.transport.abort()  # the service dies while Claude waits on the approval
    await asyncio.wait_for(task, 10)
    await manager.connection_lost()
    manager, client, task = await connect(store)
    assert row(store, sid)["connection_state"] == "connected"

    await manager.respond(sid, request_id, True, action_id="ap1")
    await until(lambda: (work / "probe.txt").exists())
    await until(lambda: row(store, sid)["runtime_state"] == "idle")
    assert (await manager.get_action("ap1")).state == "acknowledged"
    assert (await manager.get_action("l1")).state == "acknowledged"

    await manager.message(sid, "Write a 1500-word essay about tides.", action_id="m1")
    await until(lambda: row(store, sid)["runtime_state"] == "running")
    await manager.interrupt(sid, action_id="i1")
    await until(lambda: row(store, sid)["runtime_state"] == "idle", timeout=60)
    assert (await manager.get_action("i1")).state == "acknowledged"

    await manager.stop(sid, action_id="s1")
    await until(lambda: row(store, sid)["connection_state"] == "disconnected", timeout=30)
    await client.close()
    await asyncio.wait_for(task, 10)
