# Capability matrix

Dated evidence for each runtime against the PRD §16 minimum capability floor (TR-090). A runtime ships with live control only if it passes every floor item; otherwise it ships observed-only.

- **Last updated:** 2026-10-15
- **Machine:** MacBook Pro M4 Pro, macOS 26.5 (development). The release record must come from the Mac mini M4.
- **Test:** `supervisor/test_live.py <runtime>`: launch, approval held while the service is SIGKILLed, reconnect, approve, follow-up, interrupt, stop.

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
| Finding publication | Not started | Not started | Not started |
| Hosting under supervisor | Pass with scripted runtime; live pending | Pass | Pass |
| **Level so far** | **Undetermined** | **Live, pending finding publication** | **Live, pending finding publication** |

## Other capabilities (not floor items)

| Capability | Claude Code | Codex | Pi |
|---|---|---|---|
| Steer during a run | `query()` while streaming (to test) | `turn/steer` (to test) | `prompt` with `streamingBehavior: "steer"` (to test) |
| Native resume | `resume=<session_id>` (to test) | `thread/resume` (to test) | `--session <id>` (to test) |
| Terminal mode | To test | To test; Codex also runs its own daemon | To test |
| Review independence | Sandbox settings (to test) | Sandbox policy (to test) | **Not isolated**: Pi has no sandbox |
| Hook mechanism (WP-2.6) | `hooks` in `~/.claude/settings.json` | `~/.codex/hooks.json` (confirmed: 15 hook runs per session) | Extensions in `~/.pi/agent/extensions` |
| Approvals pre-empted by user config | Allow rules skip `can_use_tool` | Policy from `~/.codex/config.toml` | Only Robin's extension asks |

Per-runtime details: `claude/findings.md`, `codex/findings.md`, `pi/findings.md`.
