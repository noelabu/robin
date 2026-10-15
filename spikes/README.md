# Phase 0 spikes

Throwaway integration code for Phase 0 of `docs/implementation-plan.md`. The code is kept because its runs are the evidence behind the capability matrix. It isn't the product and won't be imported by it.

| Path | What it proves |
|---|---|
| `supervisor/` | A detached supervisor keeps runtime sessions alive across a service kill, and replays events from a durable cursor (TR-006, WP-0.2) |
| `claude/findings.md` | Claude Agent SDK results against the capability floor (WP-0.1) |

## Setup

```bash
cd spikes
uv sync                      # installs claude-agent-sdk into spikes/.venv
```

## Runs

```bash
# Supervisor with the scripted runtime: no network, no credentials
python3 supervisor/test_restart.py

# Supervisor with a real Claude Agent SDK session: costs a few cents
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env     # gitignored
uv run python supervisor/test_claude.py
```

Each run prints PASS/FAIL lines and the temp directory holding the full event spool. `test_claude.py` also writes a summary to `claude/evidence-<timestamp>.json`.

Run these on the Mac mini M4 (the reference machine) for the recorded results.
