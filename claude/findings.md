# Claude Agent SDK — Phase 0 findings

- **Runtime:** Claude Code via the Claude Agent SDK (Python)
- **Versions seen:** `claude-agent-sdk` 0.2.167, bundled Claude Code CLI 2.1.300 (same as the installed `claude`)
- **Machine so far:** MacBook Pro M4 Pro (development). Re-run on the Mac mini M4 for the recorded results.

## Capability floor (PRD §16)

| Floor item | Status | Evidence |
|---|---|---|
| Account / credentials | **Decided: API key.** | The SDK overview says: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods…" Robin uses `ANTHROPIC_API_KEY`, as the owner accepted. Subscription login was not tested. |
| Launch with native ID capture | Pending live run | `supervisor/test_live.py claude` reads `session_id` from the SDK `init` system message |
| Structured event streaming | Pending live run | `ClaudeSDKClient.receive_messages()` yields typed messages (`SystemMessage`, `AssistantMessage`, `UserMessage`, `ResultMessage`) |
| Follow-up message | Pending live run | `client.query()` on an open client continues the session |
| Interruption | Pending live run | `client.interrupt()`; the docs say to drain to the `ResultMessage` afterwards (`terminal_reason`) |
| Approval response | Pending live run | `can_use_tool` callback awaits Robin's answer. It only fires when the permission flow falls through to a prompt: allow rules, `allowed_tools` and `permission_mode` approve calls before it runs. |
| Finding publication path | Not started | Candidate: in-process SDK MCP server (`create_sdk_mcp_server`) exposing `finding.publish`, attributed by the runtime host's launch identity |
| Hosting under the supervisor | **Passed with the scripted runtime.** Claude run pending. | `supervisor/test_restart.py`, 2026-10-15: the host survived a service SIGKILL, events 1..9 replayed with no gaps or duplicates, the prompt was delivered once, an approval was answered after reconnect, a duplicate reply was rejected, interrupt was acknowledged, no processes were left behind. |

## Notes for the build

- **Settings sources.** `setting_sources=["user", "project", "local"]` loads the user's Claude Code settings, as FR-012 requires. `[]` disables filesystem settings. Passing `permission_mode="default"` explicitly keeps Robin from inheriting a more permissive default.
- **Allow rules bypass Robin's approval queue.** The owner's user settings allow `Bash(ls:*)`, `Bash(pip install:*)` and one more. Those calls never reach `can_use_tool`, so Robin can't show them as attention items. The capability report must say that approvals cover only calls the harness would prompt for. To gate every call, the docs point to a `PreToolUse` hook instead.
- **Other tools' hooks run inside Robin sessions.** The owner's user settings include `PermissionRequest` and `SessionStart` hooks from `~/.orca/agent-hooks/claude-hook.sh`, plus the owner's own hooks. Loading user settings runs them in Robin-launched sessions too. The live run shows whether the `PermissionRequest` hook answers before `can_use_tool`. This also matters for WP-2.6: Robin's own hooks must coexist with these.
- **The SDK bundles its own CLI.** `claude_agent_sdk/_bundled/claude` is used before any `claude` on `PATH`. Robin's tested version range is therefore the SDK version, not the installed CLI's. `cli_path` overrides it.
- **App identity.** Set `CLAUDE_AGENT_SDK_CLIENT_APP` in `env` so sessions identify as Robin.
- **Cost.** `ResultMessage.total_cost_usd` is a client-side estimate, cumulative across turns in streaming mode. Read the latest one; don't sum them.

## Still to test after the live run

- Native resume (`resume=` with the captured session ID) after the runtime host exits
- Terminal mode: whether `claude --resume <id>` in a terminal can take over (handoff) while Robin holds no lease
- Sandbox options that exclude the artifact store (review independence, TR-064)
- Hook mechanism and config location for WP-2.6 (`~/.claude/settings.json` `hooks`)
- Finding publication through an in-process MCP tool, with MCP disabled as the fallback check
