"""Phase 0 spike: finding publication (TR-060, TR-067).

Runtime hosts turn each runtime's publication signal into a normalized
`finding_submission` event: a `finding_publish` tool call, or a fenced
```robin-finding block in the agent's final message. Robin then builds
finding records from those events. Authorship comes from the session that
emitted the event, never from the submitted content.
"""

import hashlib
import json
import re

FENCE = re.compile(r"```robin-finding[ \t]*\n(.*?)\n```", re.S)
AGENT_FIELDS = {"submission_id", "summary", "claims", "recommendations", "questions"}
MAX_TEXT = 4000

TOOL_DESCRIPTION = (
    "Publish one structured finding to the Robin task. Robin records who published it from the "
    "session itself. Every claim needs evidence (a file path and line range) unless it sets "
    '"unsupported": true.'
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "submission_id": {"type": "string", "description": "Your own ID for this submission; reuse it only to retry the same finding"},
        "summary": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "range": {"type": "string"}}}},
            "unsupported": {"type": "boolean"}}, "required": ["text"]}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "claims"],
}

MESSAGE_INSTRUCTIONS = (
    "End your final message with exactly one fenced block tagged robin-finding containing JSON with "
    'keys "summary" (string), "claims" (list of {"text", "evidence": [{"path", "range"}]}), '
    '"recommendations" (list of strings) and "questions" (list of strings).'
)


def validate(payload) -> list[str]:
    """Return a list of problems; empty means the submission is valid."""
    if not isinstance(payload, dict):
        return ["submission must be a JSON object"]
    errors = []
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        errors.append("summary must be a non-empty string")
    elif len(payload["summary"]) > MAX_TEXT:
        errors.append(f"summary longer than {MAX_TEXT} characters")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims must be a non-empty list")
    else:
        for i, c in enumerate(claims):
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
                errors.append(f"claims[{i}].text must be a non-empty string")
                continue
            ev = c.get("evidence")
            if not c.get("unsupported") and (not isinstance(ev, list) or not ev):
                errors.append(f"claims[{i}] needs evidence or \"unsupported\": true")
            for j, e in enumerate(ev or []):
                if not isinstance(e, dict) or not (e.get("path") or e.get("artifact_id")):
                    errors.append(f"claims[{i}].evidence[{j}] needs a path or artifact_id")
    for key in ("recommendations", "questions"):
        if key in payload and not (isinstance(payload[key], list)
                                   and all(isinstance(x, str) for x in payload[key])):
            errors.append(f"{key} must be a list of strings")
    return errors


def parse_message_blocks(text: str) -> list:
    """Extract submissions from fenced robin-finding blocks; unparsable JSON becomes a marker."""
    out = []
    for raw in FENCE.findall(text or ""):
        try:
            out.append(json.loads(raw))
        except ValueError as exc:
            out.append({"_unparsable": str(exc)})
    return out


def submission_event(path: str, payload, native_ref: str) -> dict:
    """The normalized event a runtime host emits. `native_ref` identifies the native item/turn."""
    errors = ["submission is not valid JSON"] if isinstance(payload, dict) and "_unparsable" in payload \
        else validate(payload)
    return {"type": "finding_submission", "path": path, "payload": payload,
            "native_ref": native_ref, "valid": not errors, "errors": errors}


def ingest(events: list[dict], task_id: str) -> tuple[list[dict], list[dict]]:
    """Build finding records from a session's spooled events. Returns (findings, rejections)."""
    identity = {}  # robin session -> {runtime, native_session_id}
    findings, rejections, seen = [], [], {}
    for rec in events:
        e, session = rec["event"], rec["session"]
        if e["type"] == "host_spawned":
            identity.setdefault(session, {})["runtime"] = e["runtime"]
        elif e["type"] == "session_started" or (e["type"] == "sdk_message" and e.get("kind") == "SystemMessage"
                                                and e["data"].get("subtype") == "init"):
            nid = e.get("native_session_id") or e.get("data", {}).get("data", {}).get("session_id")
            identity.setdefault(session, {})["native_session_id"] = nid
        elif e["type"] == "finding_submission":
            who = identity.get(session, {})
            author = {"kind": "agent", "runtime": who.get("runtime"),
                      "native_session_id": who.get("native_session_id"), "run_id": session}
            payload = e["payload"] if isinstance(e["payload"], dict) else {}
            sub_id = payload.get("submission_id") or f"sub_{hashlib.sha256((session + e['native_ref']).encode()).hexdigest()[:10]}"
            key = (session, sub_id)
            if not e["valid"]:
                rejections.append({"submission_id": sub_id, "author": author, "errors": e["errors"],
                                   "path": e["path"], "seq": rec["seq"]})
                continue
            if key in seen:  # duplicate submission: keep the stored result
                continue
            body = {k: payload.get(k, []) for k in ("claims", "recommendations", "questions")}
            finding = {
                "schema_version": 1,
                "finding_id": "find_" + hashlib.sha256(f"{task_id}:{session}:{sub_id}".encode()).hexdigest()[:8],
                "version": 1, "submission_id": sub_id, "task_id": task_id,
                "created_at": rec["at"], "author": author, "summary": payload["summary"], **body,
                "parents": [], "publication_path": e["path"],
                "ignored_fields": sorted(set(payload) - AGENT_FIELDS),
            }
            seen[key] = finding
            findings.append(finding)
    return findings, rejections
