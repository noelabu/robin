"""Phase 0 spike: Robin's task-scoped MCP server (stdio) with `finding_publish`.

It only validates and replies, so the agent can correct a rejected submission.
Robin takes the finding from the tool-call event in the session's own stream,
which is what makes the attribution trustworthy (TR-067).
"""

from mcp.server.mcpserver import MCPServer  # mcp 2.x (was FastMCP in 1.x)
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from findings import TOOL_DESCRIPTION, validate

server = MCPServer("robin")


class Evidence(BaseModel):
    path: str = Field(description="File path relative to the working directory")
    range: str | None = Field(default=None, description='Line range, e.g. "3-4"')


class Claim(BaseModel):
    text: str
    evidence: list[Evidence] = []
    unsupported: bool = Field(default=False, description="True only when no evidence exists")


# Read-only: publishing changes nothing in the workspace, so harnesses need not ask for approval.
@server.tool(description=TOOL_DESCRIPTION,
             annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def finding_publish(summary: str, claims: list[Claim], recommendations: list[str] | None = None,
                    questions: list[str] | None = None, submission_id: str | None = None) -> str:
    payload = {"summary": summary, "claims": [c.model_dump(exclude_defaults=True) for c in claims],
               "recommendations": recommendations or [], "questions": questions or []}
    errors = validate(payload)
    if errors:
        return "REJECTED: " + "; ".join(errors) + ". Fix these and call finding_publish again."
    return "ACCEPTED: Robin recorded this finding."


if __name__ == "__main__":
    server.run()
