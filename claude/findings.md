# Claude Agent SDK — Phase 0 findings

- **Runtime:** Claude Code via the Claude Agent SDK (Python), in a per-session runtime host under the supervisor
- **Versions seen:** `claude-agent-sdk` 0.2.167, bundled Claude Code CLI 2.1.300 (same as the installed `claude`)
- **Account:** the owner's own Claude subscription through their existing `claude` login (init message: `apiKeySource: "none"`). No API key. Robin never reads or copies the token.
- **Machine so far:** MacBook Pro M4 Pro (development). Re-run on the Mac mini M4 for the recorded results.
- **Evidence:** 2026-10-15
  - `test_live.py claude`: `evidence-20261015-122611.json`
  - `test_publish.py claude tool|message`: `finding-tool-…122643.json`, `finding-message-…122958.json`
  - `test_continuity.py claude`
  - `test_isolation.py claude [deny]`

## Account decision

Robin is a personal tool, so it uses the owner's subscription (owner decision, 2026-10-15). Anthropic's Claude Code legal and compliance page:
- **Prohibits** third-party developers offering Claude.ai login in their applications, routing requests through Free/Pro/Max credentials "on behalf of their users", and collecting or intermediating credentials.
- **Allows** "an end user … signing in to the unmodified Claude Code binary with their own Claude subscription".

Robin has one user (the owner), runs the unmodified bundled binary, signs in only through `claude`'s own flow, and never touches the token. The same page asks developers building *products* on the SDK to use API keys. If Robin is ever shared, each person signs in with their own account, and an API key may be required. Pro/Max usage limits "assume ordinary, individual usage of Claude Code and the Agent SDK", so many parallel Robin sessions can hit them. The SDK emits `RateLimitEvent` messages that Robin can surface.

## Capability floor (PRD §16)

| Floor item | Status | Evidence |
|---|---|---|
| Launch with native ID capture | **Pass** | `session_id` from the SDK `init` system message. The init message also reports model, permission mode, tools, MCP servers and `apiKeySource`. |
| Structured event streaming | **Pass** | `SystemMessage`, `AssistantMessage`, `UserMessage` (tool results), `ResultMessage`, `HookEventMessage`, `RateLimitEvent` |
| Follow-up message | **Pass (new turn; steer is queued)** | `client.query()` on the open client starts the next turn. Input sent *during* a turn waits for the turn to end: the 16,142-character essay finished before Claude replied with the steered codeword. To cut a turn short, `interrupt()` first. |
| Interruption | **Pass** | `client.interrupt()` → `ResultMessage` with `subtype: "error_during_execution"` |
| Approval response | **Pass** | `can_use_tool` waited for Robin while the service was SIGKILLed. After reconnect Robin approved, and the `Write` ran. The owner's other tool's `PermissionRequest` hook did not answer first. |
| Finding publication path | **Pass, both paths** | **Tool:** in-process SDK MCP server (`create_sdk_mcp_server`) with `finding_publish`, allowed through `allowed_tools` so it needs no approval. The handler runs inside the runtime host, so attribution is that session. **Message:** a `robin-finding` block parsed from `ResultMessage.result`, with no Robin server or tool in the session (checked against the init message). |
| Hosting under the supervisor | **Pass** | The runtime host survived a service SIGKILL while Claude waited on an approval. Events stayed contiguous, the host was spawned once, and nothing was left running. |

**Classification: meets the floor. Live control.**

## Resume, handoff and isolation

- **Native resume: pass.** A new runtime host with `resume=<session_id>` got the same session ID back, and Claude remembered the codeword.
- **Terminal mode: handoff works.** With no Robin host attached, `claude -p --resume <id>` continued the same session and recalled the codeword. Interactive `claude --resume <id>` is the terminal equivalent.
- **Review independence: enforced with Robin's deny rules.**
  - With defaults, an approved `Read` of a fake unshared finding outside the working folder returned the secret.
  - With Robin's settings (passed as SDK `settings`), both routes were blocked:
    - The `Read` tool got "File is in a directory that is denied by your permission settings".
    - `cat` got "Operation not permitted" from the macOS sandbox.
  - The settings:
    ```json
    {"sandbox": {"enabled": true, "allowUnsandboxedCommands": false,
                 "filesystem": {"denyRead": ["<store>"]}},
     "permissions": {"deny": ["Read(/<store>/**)"]}}
    ```
  - Both rules are needed. The docs say the sandbox covers shell commands only, and file tools follow permission rules. `Read(...)` rules use `//` for absolute paths. Grep and Glob weren't tested separately.

## Notes for the build

- **Settings sources.** `setting_sources=["user", "project", "local"]` loads the user's Claude Code settings, as FR-012 requires. Passing `permission_mode="default"` explicitly keeps Robin from inheriting a more permissive default. The model came from the user's settings (`claude-opus-5-5[1m]`).
- **Allow rules bypass Robin's approval queue.** The owner's user settings allow `Bash(ls:*)`, `Bash(pip install:*)` and one more. Those calls never reach `can_use_tool`, so Robin can't show them as attention items. The capability report must say that approvals cover only calls the harness would prompt for. To gate every call, the docs point to a `PreToolUse` hook instead.
- **Other tools' hooks run inside Robin sessions.** The owner's user settings include hooks from `~/.orca/agent-hooks/claude-hook.sh` and the owner's own; they ran (6 `HookEventMessage`s per session). Robin's own hooks (WP-2.6) must coexist with these.
- **Hook mechanism for WP-2.6:** `hooks` in `~/.claude/settings.json`.
- **The SDK bundles its own CLI.** `claude_agent_sdk/_bundled/claude` is used before any `claude` on `PATH`. Robin's tested version range is therefore the SDK version, not the installed CLI's. `cli_path` overrides it.
- **App identity.** Set `CLAUDE_AGENT_SDK_CLIENT_APP` in `env` so sessions identify as Robin.
- **Cost.** On a subscription, `ResultMessage.total_cost_usd` is only an estimate of API-equivalent cost; usage counts against plan limits instead.
