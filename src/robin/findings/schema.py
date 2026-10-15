"""The structured finding (TR-060) and how agents submit one (TR-067).

Agents send only content: summary, claims with evidence, recommendations and unresolved questions, plus an
optional submission ID and parent artifacts. Everything that identifies the finding (ID, version, task,
author, time) is Robin's, taken from the publishing session, never from the submission.
"""

import json
import re
from typing import Any

SCHEMA_VERSION = 1
MAX_TEXT = 4000
MAX_CLAIMS = 100
FENCE = re.compile(r"```robin-finding[ \t]*\n(.*?)\n```", re.S)
AGENT_FIELDS = ("submission_id", "summary", "claims", "recommendations", "questions", "parents")

TOOL_NAME = "finding_publish"
TOOL_DESCRIPTION = (
    "Publish one structured finding to the Robin task. Robin records who published it from the session "
    'itself. Every claim needs evidence (a file path and line range) unless it sets "unsupported": true.'
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "submission_id": {
            "type": "string",
            "description": "Your own ID for this submission; reuse it only to retry the same finding",
        },
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}, "range": {"type": "string"}},
                        },
                    },
                    "unsupported": {"type": "boolean"},
                },
                "required": ["text"],
            },
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "claims"],
}
MESSAGE_INSTRUCTIONS = (
    "End your final message with exactly one fenced block tagged robin-finding containing JSON with keys "
    '"summary" (string), "claims" (list of {"text", "evidence": [{"path", "range"}]}, or "unsupported": true '
    'when you have no evidence), "recommendations" (list of strings) and "questions" (list of strings).'
)


def validate(payload: Any) -> list[str]:
    """Problems with a submission; empty means valid."""
    if not isinstance(payload, dict):
        return ["submission must be a JSON object"]
    if "_unparsable" in payload:
        return [f"submission is not valid JSON: {payload['_unparsable']}"]
    errors: list[str] = []
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    elif len(summary) > MAX_TEXT:
        errors.append(f"summary is longer than {MAX_TEXT} characters")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims must be a non-empty list")
    elif len(claims) > MAX_CLAIMS:
        errors.append(f"more than {MAX_CLAIMS} claims")
    else:
        for i, c in enumerate(claims):
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
                errors.append(f"claims[{i}].text must be a non-empty string")
                continue
            if len(c["text"]) > MAX_TEXT:
                errors.append(f"claims[{i}].text is longer than {MAX_TEXT} characters")
            ev = c.get("evidence")
            if ev is not None and not isinstance(ev, list):
                errors.append(f"claims[{i}].evidence must be a list")
                continue
            if not c.get("unsupported") and not ev:
                errors.append(f'claims[{i}] needs evidence or "unsupported": true')
            for j, e in enumerate(ev or []):
                if not isinstance(e, dict) or not (isinstance(e.get("path"), str) or e.get("artifact_id")):
                    errors.append(f"claims[{i}].evidence[{j}] needs a path or artifact_id")
    for key in ("recommendations", "questions"):
        value = payload.get(key)
        if value is not None and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
            errors.append(f"{key} must be a list of strings")
    parents = payload.get("parents")
    if parents is not None and not (isinstance(parents, list) and all(isinstance(x, str) for x in parents)):
        errors.append("parents must be a list of artifact IDs")
    elif parents and len(set(parents)) != len(parents):
        errors.append("parents lists the same artifact more than once")
    sub = payload.get("submission_id")
    if sub is not None and (not isinstance(sub, str) or not 1 <= len(sub) <= 128):
        errors.append("submission_id must be a string of 1 to 128 characters")
    return errors


def content(payload: dict[str, Any]) -> dict[str, Any]:
    """The agent-supplied part of a valid submission, normalized. Unknown fields are dropped."""
    claims = []
    for c in payload["claims"]:
        evidence = []
        for e in c.get("evidence") or []:
            item = {k: e[k] for k in ("path", "range", "artifact_id", "version") if e.get(k) is not None}
            evidence.append(item)
        claim: dict[str, Any] = {"text": c["text"], "evidence": evidence}
        if c.get("unsupported"):
            claim["unsupported"] = True
        claims.append(claim)
    return {
        "summary": payload["summary"],
        "claims": claims,
        "recommendations": list(payload.get("recommendations") or []),
        "questions": list(payload.get("questions") or []),
    }


def ignored_fields(payload: dict[str, Any]) -> list[str]:
    """Fields an agent sent that Robin doesn't take from submissions, such as a self-reported author."""
    return sorted(set(payload) - set(AGENT_FIELDS))


def parse_message_blocks(text: str) -> list[Any]:
    """Submissions in fenced robin-finding blocks; unparsable JSON becomes a marker that fails validation."""
    out: list[Any] = []
    for raw in FENCE.findall(text or ""):
        try:
            out.append(json.loads(raw))
        except ValueError as exc:
            out.append({"_unparsable": str(exc)})
    return out


def submission_event(path: str, payload: Any, native_ref: str) -> dict[str, Any]:
    """The host event a runtime host emits for one submission. `native_ref` names the native item."""
    return {"type": "finding_submission", "path": path, "payload": payload, "native_ref": native_ref}
