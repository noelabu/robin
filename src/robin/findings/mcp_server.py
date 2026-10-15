"""Robin's stdio MCP server for runtimes that take MCP servers by command (Codex).

`finding_publish` only validates and replies, so the agent can correct a rejected submission. Robin takes
the finding from the tool-call item in the session's own event stream, which is what makes attribution
trustworthy (TR-067): this process never writes anything.
"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

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


if __name__ == "__main__":
    server.run()
