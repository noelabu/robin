from robin.adapters.codex.translate import translate as codex
from robin.adapters.pi.translate import translate as pi
from robin.runtime.events import HOST_EVENTS


def types(events):
    return [e["type"] for e in events]


def test_codex_turn_lifecycle():
    assert types(codex("turn/started", {"turn": {"id": "t1"}})) == ["turn_started"]
    assert codex("turn/started", {"turn": {"id": "t1"}})[0]["native_turn_id"] == "t1"
    assert types(codex("turn/completed", {"turn": {"id": "t1", "status": "completed"}})) == ["turn_completed"]
    assert types(codex("turn/completed", {"turn": {"status": "interrupted"}})) == ["turn_interrupted"]
    assert types(codex("turn/completed", {"turn": {"status": "failed", "error": {"message": "x"}}})) == [
        "turn_failed"
    ]


def test_codex_items():
    started = codex("item/started", {"item": {"type": "commandExecution", "id": "c1", "command": "ls"}})
    assert started[0]["type"] == "tool_started" and started[0]["input"] == {"command": "ls"}
    ok = codex(
        "item/completed",
        {
            "item": {
                "type": "commandExecution",
                "id": "c1",
                "status": "completed",
                "exitCode": 0,
                "aggregatedOutput": "a\n",
            }
        },
    )
    bad = codex(
        "item/completed",
        {"item": {"type": "commandExecution", "id": "c1", "status": "completed", "exitCode": 2}},
    )
    assert types(ok) == ["tool_result"] and types(bad) == ["tool_error"]
    msg = codex(
        "item/completed",
        {"item": {"type": "agentMessage", "id": "m", "text": "DONE", "phase": "final_answer"}},
    )
    assert msg[0]["text"] == "DONE"
    fc = codex("item/completed", {"item": {"type": "fileChange", "id": "f", "changes": [{"path": "a.py"}]}})
    assert fc[0]["type"] == "file_changed" and fc[0]["paths"] == ["a.py"]
    assert codex("item/completed", {"item": {"type": "reasoning", "id": "r", "summary": ["secret"]}}) == [
        {
            "type": "codex_reasoning",
            "native_event_id": "r",
            "native_item_type": "reasoning",
            "native_method": "item/completed",
        }
    ]  # reasoning content isn't kept
    assert types(codex("account/rateLimits/updated", {})) == ["codex_notification"]


def test_pi_records():
    assert types(pi({"type": "agent_start"})) == ["turn_started"]
    assert types(pi({"type": "agent_settled", "aborted": True})) == ["turn_interrupted"]
    assert types(pi({"type": "agent_settled", "aborted": False})) == ["turn_completed"]
    assert (
        pi({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hi"}})[0][
            "text"
        ]
        == "Hi"
    )
    end = pi(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "DONE"}]},
        }
    )
    assert end[0]["type"] == "message_completed" and end[0]["text"] == "DONE"
    tool = pi(
        {"type": "tool_execution_start", "toolCallId": "c", "toolName": "bash", "args": {"command": "ls"}}
    )
    assert tool[0]["type"] == "tool_started" and tool[0]["tool"] == "bash"
    assert types(pi({"type": "tool_execution_end", "toolCallId": "c", "isError": True, "result": {}})) == [
        "tool_error"
    ]
    assert pi({"type": "turn_end"}) == [] and types(pi({"type": "queue_update"})) == ["pi_event"]


def test_all_translated_types_are_in_the_host_vocabulary():
    produced = {
        "turn_started",
        "turn_completed",
        "turn_interrupted",
        "turn_failed",
        "tool_started",
        "tool_result",
        "tool_error",
        "message_completed",
        "message_delta",
        "user_message",
        "file_changed",
    }
    assert produced <= set(HOST_EVENTS)
