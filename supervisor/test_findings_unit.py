"""Offline checks for finding validation, message parsing and attribution.

    uv run python test_findings_unit.py
"""

import json

from findings import ingest, parse_message_blocks, submission_event, validate

GOOD = {"summary": "Auth is underspecified.",
        "claims": [{"text": "Tenant comes from a header.", "evidence": [{"path": "PRD.md", "range": "3-4"}]}],
        "recommendations": ["Use token claims."], "questions": []}


def ev(seq, session, event):
    return {"seq": seq, "session": session, "at": 1.0, "event": event}


def main():
    assert validate(GOOD) == []
    assert "claims[0] needs evidence" in validate({"summary": "x", "claims": [{"text": "y"}]})[0]
    assert validate({"summary": "x", "claims": [{"text": "y", "unsupported": True}]}) == []
    assert validate({"summary": "", "claims": []}) == ["summary must be a non-empty string",
                                                       "claims must be a non-empty list"]

    forged = {**GOOD, "author": {"kind": "agent", "runtime": "claude"}}
    blocks = parse_message_blocks("Done.\n```robin-finding\n" + json.dumps(forged) + "\n```\n")
    assert len(blocks) == 1
    assert parse_message_blocks("```robin-finding\n{not json\n```")[0].get("_unparsable")

    findings, rejected = ingest([
        ev(1, "s1", {"type": "host_spawned", "runtime": "pi"}),
        ev(2, "s1", {"type": "session_started", "native_session_id": "native-pi"}),
        ev(3, "s1", submission_event("message", blocks[0], "m1")),  # content claims to be from claude
        ev(4, "s1", submission_event("message", blocks[0], "m1")),  # replay of the same message
        ev(5, "s1", submission_event("tool", {"summary": "", "claims": []}, "t1")),
    ], "task_01")
    assert len(findings) == 1, findings
    assert findings[0]["author"] == {"kind": "agent", "runtime": "pi", "native_session_id": "native-pi",
                                     "run_id": "s1"}
    assert findings[0]["ignored_fields"] == ["author"]
    assert len(rejected) == 1 and rejected[0]["errors"]
    print("PASS validation, message parsing, attribution from the session (claimed author ignored), "
          "duplicate dropped, invalid submission rejected")


if __name__ == "__main__":
    main()
