"""The supervisor with the scripted runtime: sessions survive the service, replay is exact (TR-006)."""

import asyncio
import os
import signal
import time

import pytest

from robin.events.ingest import ensure_session, read_events
from robin.supervisor.client import SupervisorBridge, SupervisorClient, committed_cursor, ensure_supervisor
from robin.supervisor.server import supervisor_dir


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _supervisor_pid(data_dir):
    return int((supervisor_dir(data_dir) / "supervisor.lock").read_text())


@pytest.fixture
def supervised(store, monkeypatch):
    monkeypatch.setenv("ROBIN_FAKE_TICK", "0.05")
    asyncio.run(ensure_supervisor(store.data_dir))
    yield store
    pid = _supervisor_pid(store.data_dir)  # whichever supervisor holds the lock now
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
    os.system("pkill -f 'robin.runtime.host --runtime fake' >/dev/null 2>&1")


async def _events_until(client, pred, timeout=10.0):
    got = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        msg = await asyncio.wait_for(client.events.get(), max(remaining, 0.01))
        got.append(msg)
        if pred(msg):
            return got


async def test_session_survives_service_loss_and_replays_exactly(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    a = SupervisorClient(store.data_dir)
    await a.connect(after=0)
    reply = await a.request({"type": "launch", "session": sid, "runtime": "fake", "prompt": "go"})
    assert reply["ok"]
    pid = reply["pid"]
    seen = await _events_until(a, lambda m: m["event"].get("type") == "tick" and m["event"]["n"] == 2)
    cursor = seen[-1]["seq"]
    a._writer.transport.abort()  # the service dies without saying goodbye

    await asyncio.sleep(0.6)  # the host keeps ticking into the spool, then waits on its approval
    assert _alive(pid)

    b = SupervisorClient(store.data_dir)
    hello = await b.connect(after=cursor)
    assert hello["sessions"][sid]["alive"]
    replay = await _events_until(b, lambda m: m["event"].get("type") == "approval_request")
    assert [m["seq"] for m in replay] == list(range(cursor + 1, cursor + 1 + len(replay)))
    req = replay[-1]["event"]["request_id"]
    assert (
        await b.request(
            {
                "type": "command",
                "session": sid,
                "command": {"cmd": "approval", "request_id": req, "allow": True},
            }
        )
    )["ok"]
    await _events_until(b, lambda m: m["event"].get("type") == "tool_decision")
    await b.request({"type": "command", "session": sid, "command": {"cmd": "interrupt", "action_id": "a1"}})
    ack = await _events_until(b, lambda m: m["event"].get("type") == "interrupt_acknowledged")
    assert ack[-1]["event"]["action_id"] == "a1"
    await b.request({"type": "command", "session": sid, "command": {"cmd": "stop"}})
    await _events_until(b, lambda m: m["event"].get("type") == "host_exited")
    await b.close()


async def test_bridge_ingests_and_acks_after_commit(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    client = SupervisorClient(store.data_dir)
    await client.connect(after=committed_cursor(store))
    await client.request({"type": "launch", "session": sid, "runtime": "fake", "prompt": "go"})
    bridge = SupervisorBridge(store, client)
    task = asyncio.create_task(bridge.run())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if any(e.event_type == "unknown.raw" for e in read_events(store)) and bridge.cursor >= 6:
            break
    status = await client.request({"type": "status"})
    assert status["acked"] >= 6 and committed_cursor(store) == bridge.cursor
    seqs = [e.source_seq for e in read_events(store) if e.event_type == "unknown.raw"]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    await client.request({"type": "command", "session": sid, "command": {"cmd": "stop"}})
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_shutdown_requires_naming_the_sessions_it_ends(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    c = SupervisorClient(store.data_dir)
    await c.connect(after=0)
    await c.request({"type": "launch", "session": sid, "runtime": "fake"})
    refused = await c.request({"type": "shutdown", "confirm_sessions": []})
    assert refused == {**refused, "ok": False, "error": "confirm_sessions_mismatch", "would_end": [sid]}
    ok = await c.request({"type": "shutdown", "confirm_sessions": [sid]})
    assert ok["ok"] and ok["ending"] == [sid]
    await c.closed.wait()


async def test_supervisor_crash_ends_hosts_and_nothing_relaunches(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    c = SupervisorClient(store.data_dir)
    await c.connect(after=0)
    host_pid = (await c.request({"type": "launch", "session": sid, "runtime": "fake", "prompt": "go"}))["pid"]
    os.kill(_supervisor_pid(store.data_dir), signal.SIGKILL)
    await c.closed.wait()
    for _ in range(100):  # the host's stdin closes; it stops on its own
        if not _alive(host_pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(host_pid)
    await ensure_supervisor(store.data_dir)  # a new supervisor starts empty
    d = SupervisorClient(store.data_dir)
    hello = await d.connect(after=0)
    assert hello["sessions"] == {}
    # events spooled before the crash are still delivered (new generation, same global seq)
    first = await asyncio.wait_for(d.events.get(), 5)
    assert first["event"]["type"] == "host_spawned"
    await d.close()


async def test_rejects_wrong_protocol_version(supervised):
    from robin.supervisor.protocol import encode
    from robin.supervisor.server import socket_path

    r, w = await asyncio.open_unix_connection(str(socket_path(supervised.data_dir)))
    w.write(encode({"type": "hello", "protocol": 999, "after": 0}))
    await w.drain()
    assert b"hello_rejected" in await r.readline()
    w.close()


async def test_failed_launch_replies_and_keeps_connection(supervised):
    store = supervised
    sid = ensure_session(store, runtime="fake", mode="managed")
    c = SupervisorClient(store.data_dir)
    await c.connect(after=0)
    bad = await c.request(
        {"type": "launch", "session": sid, "runtime": "fake", "cwd": "/no/such/dir"}, timeout=5
    )
    assert bad["ok"] is False and "error" in bad
    junk = await c.request({"type": "command"}, timeout=5)  # missing fields
    assert junk["ok"] is False
    assert (await c.request({"type": "status"}, timeout=5))["ok"]
    await c.close()


def test_long_data_dir_socket_lives_in_private_directory(tmp_path):
    from robin.supervisor.server import socket_path

    deep = tmp_path / ("x" * 120)
    sock = socket_path(deep)
    assert len(str(sock)) < 100
    assert sock.parent.stat().st_mode & 0o777 == 0o700 and sock.parent.stat().st_uid == os.getuid()
