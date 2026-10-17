# Capability matrix

Dated evidence for each runtime against the PRD §16 minimum capability floor (TR-090). A runtime ships with live control only if it passes every floor item; otherwise it ships observed-only.

- **Last updated:** 2026-10-15
- **Machine:** MacBook Pro M4 Pro, macOS 26.5, the reference machine (owner decision 2026-10-16). Release results: `docs/release.md`.
- **Tests:** `supervisor/test_isolation.py <runtime> [deny]`: can a reviewer read Robin's artifact store. `supervisor/test_continuity.py <runtime>`: steer mid-turn, resume after the host exits, continue from the native CLI. `supervisor/test_live.py <runtime>`: launch, approval held while the service is SIGKILLed, reconnect, approve, follow-up, interrupt, stop. `supervisor/test_publish.py <runtime> tool|message`: the agent reviews a PRD and publishes one finding; Robin checks schema, evidence and attribution. `supervisor/test_findings_unit.py`: offline validation, parsing, attribution and duplicate checks.

## Floor

| Floor item | Claude Code (Agent SDK) | Codex (app-server) | Pi (RPC) |
|---|---|---|---|
| Version tested | SDK 0.2.167, bundled CLI 2.1.300 | codex-cli 0.162.1 | pi-coding-agent 1.2.0 |
| Account | Owner's Claude subscription via `claude` login (personal tool; no API key) | ChatGPT login | Pi provider config (OpenRouter API key) |
| Launch + native ID | Pass | Pass | Pass |
| Structured events | Pass | Pass | Pass |
| Follow-up | Pass (new turn) | Pass (new turn) | Pass (new run) |
| Interrupt | Pass | Pass | Pass |
| Approval response | Pass (native `can_use_tool`) | Pass (native) | Pass (Robin extension; Pi has no native approvals) |
| Finding publication | Pass: in-process MCP tool, and message block without MCP | Pass: MCP tool, and message block without MCP | Pass: extension tool, and message block |
| Hosting under supervisor | Pass | Pass | Pass |
| **Level so far** | **Live** | **Live** | **Live** (approvals via Robin's extension) |

## Other capabilities (not floor items)

| Capability | Claude Code | Codex | Pi |
|---|---|---|---|
| Steer during a run | **Queued to the next turn boundary**; use `interrupt()` + `query()` to cut a turn short | **Pass, applied mid-turn** (`turn/steer`) | **Queued to the next turn boundary** (`streamingBehavior: "steer"`); use `abort` + `prompt` to cut a turn short |
| Native resume | **Pass** (`resume=<session_id>`) | **Pass** (`thread/resume`) | **Pass** (`--session <id>`) |
| Terminal mode | **Handoff works** (`claude --resume <id>`) | **Handoff works** (`codex resume <id>`). **Attach possible** if Robin hosts Codex behind a listener; every client can drive, so it's shared control | **Handoff works** (`pi --session <id>`); no attach |
| Review independence | **Enforced with deny rules**: sandbox `denyRead` for shell + `Read(//…)` deny for file tools (defaults do not isolate) | **Enforced with a deny profile** on the artifact store (default read-only sandbox does not isolate) | **Not isolated**: Pi has no sandbox |
| Hook mechanism (WP-2.6) | `hooks` in `~/.claude/settings.json` | `~/.codex/hooks.json` (confirmed: 15 hook runs per session) | Extensions in `~/.pi/agent/extensions` |
| Skip permission prompts (opt-in per session) | `permission_mode=bypassPermissions`; deny rules and the sandbox still apply | `approvalPolicy: never`; the sandbox is unchanged, so blocked commands fail instead of asking | Refused: Pi has no permission prompts |
| Approvals pre-empted by user config | Allow rules skip `can_use_tool` | Policy from `~/.codex/config.toml` | Only Robin's extension asks |

Per-runtime details: `claude/findings.md`, `codex/findings.md`, `pi/findings.md`.
