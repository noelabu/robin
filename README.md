# Robin

A local workspace for running, observing, and coordinating coding agents (Claude Code, Codex, Pi).

- Product requirements: `docs/Robin_PRD.docx` (v0.5)
- Technical requirements: `docs/Robin_Technical_Requirements.docx` (v0.4)
- Implementation plan: `docs/implementation-plan.md`

## Current state: Phase 0 (integration validation)

The code here is Phase 0 spike code. Its runs are the evidence behind the capability matrix; the product code comes in Phase 1.

| Path | What it proves |
|---|---|
| `supervisor/` | A detached supervisor keeps runtime sessions alive across a service kill and replays events from a durable cursor (TR-006, WP-0.2). Runtime hosts for a scripted runtime, the Claude Agent SDK, `codex app-server` and `pi --mode rpc`. |
| `claude/findings.md` | Claude Agent SDK results against the capability floor (WP-0.1) |
| `codex/findings.md` | Codex app-server results against the capability floor (WP-0.1) |
| `pi/findings.md` | Pi RPC results against the capability floor (WP-0.1) |
| `docs/capability-matrix.md` | All three runtimes side by side |

## Setup

```bash
uv sync                      # installs claude-agent-sdk into .venv
```

## Runs

```bash
# Supervisor with the scripted runtime: no network, no credentials
python3 supervisor/test_restart.py

# Supervisor with a real Claude Agent SDK session: costs a few cents
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env     # gitignored
uv run python supervisor/test_live.py claude

# Supervisor with a real Codex app-server session: uses your Codex login
uv run python supervisor/test_live.py codex

# Supervisor with a real Pi RPC session: uses Pi's configured provider
uv run python supervisor/test_live.py pi

# Finding publication: tool path (MCP / extension tool) or message path (no MCP)
uv run python supervisor/test_publish.py codex tool
uv run python supervisor/test_publish.py pi message
uv run python supervisor/test_findings_unit.py     # offline
```

Each run prints PASS/FAIL lines and the temp directory holding the full event spool. `test_live.py` also writes a summary to `<runtime>/evidence-<timestamp>.json`.

Run these on the Mac mini M4 (the reference machine) for the recorded results.
