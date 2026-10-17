"""Native terminal handoff and the control lease (FR-037, TR-046) with the scripted runtime."""

import asyncio
import subprocess
import sys

import pytest

from robin.events.ingest import ensure_session
from robin.runtime import attention
from robin.runtime.ledger import ActionRejected
from robin.runtime.terminal import holders, resume_command
from test_manager import connect, session_row, wait_for


async def settled(manager, action_id, state="acknowledged", timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while (await manager.get_action(action_id)).state != state:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(await manager.get_action(action_id))
        await asyncio.sleep(0.05)


async def launched(store, prompt=None):
    sid = ensure_session(store, runtime="fake", mode="managed", execution_cwd=str(store.data_dir.parent))
    manager, client, task = await connect(store)
    await manager.launch(
        sid, runtime="fake", cwd=str(store.data_dir.parent), action_id="launch-1", prompt=prompt
    )
    await wait_for(lambda: session_row(store, sid)["native_session_id"] is not None)
    return sid, manager, client, task


async def test_handoff_releases_the_session_and_blocks_robin_controls(supervised):
    store = supervised
    sid, manager, client, task = await launched(store)
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "idle")
    gen = session_row(store, sid)["lease_generation"]
    await manager.handoff(sid, action_id="h1")
    await settled(manager, "h1")  # acknowledged once Robin's host has exited
    row = session_row(store, sid)
    assert row["control_owner"] == "terminal" and row["lease_generation"] == gen + 1
    assert row["connection_state"] == "disconnected"
    with pytest.raises(ActionRejected, match="handed off"):
        await manager.message(sid, "hi", action_id="m1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_handoff_refuses_a_running_turn_unless_confirmed(supervised):
    store = supervised
    sid, manager, client, task = await launched(store, prompt="go")
    await wait_for(lambda: session_row(store, sid)["runtime_state"] in ("running", "waiting_for_input"))
    with pytest.raises(ActionRejected, match="turn is running"):
        await manager.handoff(sid, action_id="h1")
    await wait_for(lambda: bool(attention.list_items(store, session_id=sid)))  # the fake asks for approval
    await manager.handoff(sid, action_id="h2", interrupt_running=True)
    await settled(manager, "h2")
    (item,) = attention.list_items(store, states=("handed_off", "expired"), session_id=sid)
    with pytest.raises(attention.AttentionError):  # no duplicate response from Robin after handoff
        attention.claim(store, item["attention_id"], "act-xxxxxxxx")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_reclaim_waits_for_the_terminal_and_resumes(supervised):
    store = supervised
    sid, manager, client, task = await launched(store)
    await wait_for(lambda: session_row(store, sid)["runtime_state"] == "idle")
    native = session_row(store, sid)["native_session_id"]
    await manager.handoff(sid, action_id="h1")
    await settled(manager, "h1")
    terminal = subprocess.Popen(  # a terminal still on the session, its id in the command line
        [sys.executable, "-c", "import time; time.sleep(30)", "--resume", native]
    )
    try:
        assert holders(native)
        with pytest.raises(ActionRejected, match="close the terminal"):
            await manager.reclaim(sid, action_id="r1")
    finally:
        terminal.kill()
        terminal.wait()
    gen = session_row(store, sid)["lease_generation"]
    await manager.reclaim(sid, action_id="r2")
    await settled(manager, "r2")
    row = session_row(store, sid)
    assert row["control_owner"] == "robin" and row["lease_generation"] == gen + 1
    await wait_for(lambda: session_row(store, sid)["connection_state"] == "connected")
    await manager.message(sid, "back in Robin", action_id="m2")
    await settled(manager, "m2")
    await manager.stop(sid, action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


def test_resume_commands_are_argument_arrays_with_validated_ids():
    assert resume_command("claude", "abc-123") == ["claude", "--resume", "abc-123"]
    assert resume_command("codex", "019a-1") == ["codex", "resume", "019a-1"]
    assert resume_command("pi", "x") == ["pi", "--session", "x"]
    # a session launched with prompts skipped keeps them skipped in the terminal
    assert resume_command("claude", "abc-123", skip_permissions=True) == [
        "claude",
        "--resume",
        "abc-123",
        "--permission-mode",
        "bypassPermissions",
    ]
    assert resume_command("codex", "019a-1", skip_permissions=True) == [
        "codex",
        "resume",
        "019a-1",
        "-a",
        "never",
    ]
    with pytest.raises(ValueError):
        resume_command("claude", "abc; echo injected")


@pytest.mark.parametrize("native_id", ["--dangerously-skip-permissions", "-x", ".", ""])
@pytest.mark.parametrize("runtime", ["claude", "codex", "pi"])
def test_a_native_id_that_could_be_a_flag_never_becomes_a_command(runtime, native_id):
    # Observed ids come from transcript contents, which an agent can write.
    with pytest.raises(ValueError):
        resume_command(runtime, native_id)
    assert holders(native_id) == []
