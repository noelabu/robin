"""Robin's stdio MCP server for runtimes that take MCP servers by command (Codex).

`finding_publish` only validates and replies, so the agent can correct a rejected submission. Robin takes
the finding from the tool-call item in the session's own event stream, which is what makes attribution
trustworthy (TR-067): this process never writes anything.
"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from robin import agent_client
from robin.findings import schema

server = MCPServer("robin")


class Evidence(BaseModel):
    path: str = Field(description="File path relative to the working directory")
    range: str | None = Field(default=None, description='Line range, e.g. "3-4"')


class Claim(BaseModel):
    text: str
    evidence: list[Evidence] = []
    unsupported: bool = Field(default=False, description="True only when no evidence exists")


# Read-only: publishing changes nothing in the workspace, so harnesses need not ask for approval.
@server.tool(
    name=schema.TOOL_NAME,
    description=schema.TOOL_DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def finding_publish(
    summary: str,
    claims: list[Claim],
    recommendations: list[str] | None = None,
    questions: list[str] | None = None,
    submission_id: str | None = None,
) -> str:
    payload = {
        "summary": summary,
        "claims": [c.model_dump(exclude_defaults=True) for c in claims],
        "recommendations": recommendations or [],
        "questions": questions or [],
    }
    errors = schema.validate(payload)
    if errors:
        return "REJECTED: " + "; ".join(errors) + f". Fix these and call {schema.TOOL_NAME} again."
    return "ACCEPTED: Robin recorded this finding."


# Retrieval tools (TR-066): task- and session-scoped by the session's own credential, granted sources only.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@server.tool(
    name="task_get", description=agent_client.TOOLS["task_get"]["description"], annotations=_READ_ONLY
)
def task_get() -> str:
    return agent_client.call("task_get", {})


@server.tool(
    name="finding_list", description=agent_client.TOOLS["finding_list"]["description"], annotations=_READ_ONLY
)
def finding_list() -> str:
    return agent_client.call("finding_list", {})


@server.tool(
    name="finding_read", description=agent_client.TOOLS["finding_read"]["description"], annotations=_READ_ONLY
)
def finding_read(finding_id: str) -> str:
    return agent_client.call("finding_read", {"finding_id": finding_id})


@server.tool(
    name="session_search",
    description=agent_client.TOOLS["session_search"]["description"],
    annotations=_READ_ONLY,
)
def session_search(query: str) -> str:
    return agent_client.call("session_search", {"query": query})


@server.tool(
    name="session_read_excerpt",
    description=agent_client.TOOLS["session_read_excerpt"]["description"],
    annotations=_READ_ONLY,
)
def session_read_excerpt(session_id: str, after: int, through: int) -> str:
    return agent_client.call(
        "session_read_excerpt", {"session_id": session_id, "after": after, "through": through}
    )


@server.tool(
    name="artifact_publish",
    description=agent_client.TOOLS["artifact_publish"]["description"],
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def artifact_publish(title: str, text: str, inputs: list[str] | None = None) -> str:
    # Writes only to Robin's own store, never to the workspace, so harnesses need not ask for approval.
    return agent_client.call("artifact_publish", {"title": title, "text": text, "inputs": inputs or []})


if __name__ == "__main__":
    server.run()
