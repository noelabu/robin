from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from robin.adapters.claude.translate import translate
from robin.runtime.events import HOST_EVENTS


def result(**kw):
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="s-1",
        total_cost_usd=0.01,
        result="DONE",
    )
    return ResultMessage(**{**base, **kw})


def test_init_gives_native_session_and_account_source():
    (ev,) = translate(
        SystemMessage(
            subtype="init",
            data={"session_id": "abc", "apiKeySource": "none", "model": "m", "permissionMode": "default"},
        )
    )
    assert ev["type"] == "session_started" and ev["native_session_id"] == "abc"
    assert ev["api_key_source"] == "none" and ev["permission_mode"] == "default"


def test_assistant_blocks_become_message_and_tool_events():
    msg = AssistantMessage(
        content=[
            TextBlock("hello"),
            ThinkingBlock("secret reasoning", "sig"),
            ToolUseBlock("tu1", "Write", {"file_path": "x"}),
        ],
        model="m",
    )
    evs = translate(msg)
    assert [e["type"] for e in evs] == ["message_completed", "claude_thinking", "tool_started"]
    assert "secret reasoning" not in str(evs)  # thinking content isn't kept
    assert evs[2]["tool_call_id"] == "tu1" and evs[2]["input"] == {"file_path": "x"}


def test_tool_results_and_errors():
    msg = UserMessage(content=[ToolResultBlock("tu1", "ok", False), ToolResultBlock("tu2", "boom", True)])
    assert [e["type"] for e in translate(msg)] == ["tool_result", "tool_error"]


def test_result_maps_to_turn_end_variants():
    assert translate(result())[0]["type"] == "turn_completed"
    assert translate(result(subtype="error_during_execution", is_error=True))[0]["type"] == "turn_failed"
    assert (
        translate(result(subtype="error_during_execution", is_error=True), interrupted=True)[0]["type"]
        == "turn_interrupted"
    )


def test_unknown_messages_degrade_and_every_mapped_type_is_known():
    class BrandNewMessage:
        pass

    assert translate(BrandNewMessage()) == [{"type": "claude_message", "native_kind": "BrandNewMessage"}]
    produced = {
        "session_started",
        "message_completed",
        "tool_started",
        "tool_result",
        "tool_error",
        "turn_completed",
        "turn_failed",
        "turn_interrupted",
        "user_message",
    }
    assert produced <= set(HOST_EVENTS)


BROKEN_STREAM = """
import claude_agent_sdk

class FakeClient:
    def __init__(self, options=None):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def receive_messages(self):
        raise RuntimeError("stream broke")
        yield
    async def query(self, text):
        pass
    async def interrupt(self):
        pass

claude_agent_sdk.ClaudeSDKClient = FakeClient
import robin.adapters.claude.host
import robin.runtime.host as h
h.main(["--runtime", "claude", "--cwd", "."])
"""


def test_broken_event_stream_ends_the_host_with_an_error(tmp_path):
    """If the SDK stream fails, the host reports it and exits instead of hanging (review R20261015-144101)."""
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", BROKEN_STREAM],
        cwd=tmp_path,
        input='{"cmd": "send", "text": "x"}\n',
        capture_output=True,
        text=True,
        timeout=30,
    )
    types = [json.loads(line)["type"] for line in proc.stdout.splitlines()]
    assert "host_error" in types and types[-1] == "session_stopping", (types, proc.stderr[-500:])
