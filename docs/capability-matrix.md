# Capability matrix

Dated evidence for each runtime against the PRD §16 minimum capability floor (TR-090). A runtime ships with live control only if it passes every floor item; otherwise it ships observed-only.

- **Last updated:** 2026-10-15
- **Machine:** MacBook Pro M4 Pro, macOS 26.5 (development). The release record must come from the Mac mini M4.
- **Tests:** `supervisor/test_continuity.py <runtime>`: steer mid-turn, resume after the host exits, continue from the native CLI. `supervisor/test_live.py <runtime>`: launch, approval held while the service is SIGKILLed, reconnect, approve, follow-up, interrupt, stop. `supervisor/test_publish.py <runtime> tool|message`: the agent reviews a PRD and publishes one finding; Robin checks schema, evidence and attribution. `supervisor/test_findings_unit.py`: offline validation, parsing, attribution and duplicate checks.

## Floor

| Floor item | Claude Code (Agent SDK) | Codex (app-server) | Pi (RPC) |
|---|---|---|---|
| Version tested | SDK 0.2.167, bundled CLI 2.1.300 | codex-cli 0.162.1 | pi-coding-agent 1.2.0 |
| Account | API key (required by Anthropic for third-party tools) | ChatGPT login | Pi provider config (OpenRouter API key) |
| Launch + native ID | Pending: needs API key | Pass | Pass |
| Structured events | Pending | Pass | Pass |
| Follow-up | Pending | Pass (new turn) | Pass (new run) |
| Interrupt | Pending | Pass | Pass |
| Approval response | Pending | Pass (native) | Pass (Robin extension; Pi has no native approvals) |
| Finding publication | Code ready; pending API key | Pass: MCP tool, and message block without MCP | Pass: extension tool, and message block |
| Hosting under supervisor | Pass with scripted runtime; live pending | Pass | Pass |
| **Level so far** | **Undetermined (needs API key)** | **Live** | **Live** (approvals via Robin's extension) |

## Other capabilities (not floor items)

| Capability | Claude Code | Codex | Pi |
|---|---|---|---|
| Steer during a run | `query()` while streaming (to test) | **Pass, applied mid-turn** (`turn/steer`) | **Queued to the next turn boundary** (`streamingBehavior: "steer"`); use `abort` + `prompt` to cut a turn short |
| Native resume | `resume=<session_id>` (code ready; to test) | **Pass** (`thread/resume`) | **Pass** (`--session <id>`) |
| Terminal mode | To test (`claude --resume <id>`) | **Handoff works** (`codex resume <id>`); attach through Codex's daemon untested | **Handoff works** (`pi --session <id>`); no attach |
| Review independence | Sandbox settings (to test) | Sandbox policy (to test) | **Not isolated**: Pi has no sandbox |
| Hook mechanism (WP-2.6) | `hooks` in `~/.claude/settings.json` | `~/.codex/hooks.json` (confirmed: 15 hook runs per session) | Extensions in `~/.pi/agent/extensions` |
| Approvals pre-empted by user config | Allow rules skip `can_use_tool` | Policy from `~/.codex/config.toml` | Only Robin's extension asks |

Per-runtime details: `claude/findings.md`, `codex/findings.md`, `pi/findings.md`.
