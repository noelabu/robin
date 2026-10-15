# Codex app-server — Phase 0 findings

- **Runtime:** Codex via `codex app-server` (stdio JSON-RPC), spawned by a per-session runtime host under the supervisor
- **Versions seen:** `codex-cli` 0.162.1. Protocol schemas come from `codex app-server generate-json-schema`.
- **Account:** the existing ChatGPT login (`codex login status`: "Logged in using ChatGPT"). No API key needed; usage counts against the ChatGPT plan.
- **Machine so far:** MacBook Pro M4 Pro (development). Re-run on the Mac mini M4 for the recorded results.
- **Evidence:** `supervisor/test_live.py codex`, two runs on 2026-10-15 (`evidence-20261015-101845.json`, `evidence-20261015-102056.json`). Both passed every check.

## Capability floor (PRD §16)

| Floor item | Status | Evidence |
|---|---|---|
| Launch with native ID capture | **Pass** | `thread/start` returns `thread.id` (UUIDv7), also sent in `thread/started`. The resolved approval policy, sandbox and model come back in the response. |
| Structured event streaming | **Pass** | Typed notifications: `thread/*`, `turn/started`, `turn/completed`, `item/started`, `item/completed`, `item/agentMessage/delta`, `hook/started`, `hook/completed`, `thread/tokenUsage/updated`, `account/rateLimits/updated`, `serverRequest/resolved`, and more |
| Follow-up message | **Pass (new turn and steer)** | `turn/start` on the existing thread. `turn/steer` with `expectedTurnId` during an essay turn: Codex dropped the essay and replied only with the steered codeword, so steering takes effect mid-turn (`test_continuity.py codex`). |
| Interruption | **Pass** | `turn/interrupt {threadId, turnId}` → `turn/completed` with `turn.status: "interrupted"` |
| Approval response | **Pass** | `item/commandExecution/requestApproval` arrives as a server request carrying `threadId`, `turnId`, `itemId` and the command. Answered with `{"decision": "accept"}` after the service was SIGKILLed and reconnected. `serverRequest/resolved` confirms it. Decisions: `accept`, `acceptForSession`, `decline`, `cancel` (decline and interrupt the turn), plus policy amendments, which Robin must not offer by default. File changes use `item/fileChange/requestApproval` with the same decisions. |
| Finding publication path | **Pass, both paths** | **Tool:** Robin's MCP server (`supervisor/robin_mcp.py`), added for one thread through `thread/start` `config.mcp_servers.robin`. Robin takes the finding from the completed `mcpToolCall` item in the session's own stream. **Message:** with no MCP server, a fenced `robin-finding` block in the `final_answer` agent message. Both runs produced one valid finding with PRD.md line evidence, attributed to the Codex thread (`finding-tool-20261015-104655.json`, `finding-message-20261015-104742.json`). |
| Hosting under the supervisor | **Pass** | The host and its `codex app-server` child survived a service SIGKILL while waiting on an approval. Events stayed contiguous across the restart, the host was spawned once, and nothing was left running after shutdown. |

**Classification: meets the floor. Live control.**

## Notes for the build

- **Approval policy.** The test asks for `approvalPolicy: "untrusted"`, which is stricter than the default, so the command needs approval. The resolved sandbox was `readOnly` with no network. The runtime host otherwise sends no policy and inherits `~/.codex/config.toml`, as FR-012 requires. Still to check: what the inherited default resolves to.
- **Approval IDs.** The native correlation is the JSON-RPC request ID plus `threadId`/`turnId`/`itemId`. Some requests also carry an `approvalId`. Robin's attention item should keep all of them (TR-045).
- **Codex runs the user's own hooks.** 15 hook runs per session came from `~/.codex/hooks.json`, and they are visible as `hook/started` and `hook/completed`. Codex has a hook mechanism with a config file, and `hooks/list` reports it. This answers the Phase 0 hook question for WP-2.6.
- **Codex already has its own daemon.** A `codex app-server --listen unix:// --managed-daemon` process (and `codex app-server daemon` / `proxy` subcommands) runs on this machine independently of Robin, likely from the Codex desktop app. Robin could attach to it instead of spawning its own server. That would matter for terminal handoff and for observing sessions started elsewhere. Investigate before deciding the terminal mode.
- **Protocol surface is large and changes often.** It has 100+ client methods, many marked experimental. Pin the tested `codex-cli` version and generate typed bindings from `generate-json-schema` per version (TRD integration candidates).
- **Unhandled server requests** are refused with a JSON-RPC error, never left hanging. None occurred in these runs.

## Resume and terminal handoff (2026-10-15, `test_continuity.py codex`)

- **Native resume: pass.** After the runtime host exited, a new host called `thread/resume {threadId}`. It got the same thread ID back, and the agent remembered the codeword from before.
- **Terminal mode: handoff works.** With no Robin host attached, `codex exec resume <id> "<prompt>"` continued the same thread and recalled the codeword. Interactive `codex resume <id>` is the terminal equivalent. Outside a git repo or trusted folder, `codex exec` refuses to run without `--skip-git-repo-check`; real project folders are git repos.

## Review independence (2026-10-15, `test_isolation.py codex [deny]`)

- **The default sandbox does not isolate reviewers.** Under `:read-only` with approvals off, the agent ran `cat` on a fake unshared finding outside its working folder and read the secret. Codex's read-only sandbox limits writes, not reads.
- **A deny profile does.** The runtime host passes a per-thread config override:
  `default_permissions = "robin_review"`, with `permissions.robin_review` set to `{extends = ":read-only", filesystem = {"<artifact store>" = "deny"}}`.
  - In the thread, Codex told the model the path was denied by policy, and nothing was read.
  - Run directly with `codex sandbox` and the same profile, `cat` on the store failed with "Operation not permitted" (exit 1), while other files stayed readable. So macOS Seatbelt enforces it; it isn't just the model complying.
- **For the build:** launch Codex reviewers with this profile on Robin's artifact store and report independence as **enforced**. The config needs an absolute, symlink-resolved path. On macOS `/var/...` resolves to `/private/var/...`. Keys containing dots must be written as an inline table, not dotted `-c` keys.

## Finding publication notes

- **The tool schema has to be typed.** The first run declared `claims` as a list of plain objects, and the model invented its own keys (`claim`, `file_path`, `line_start`), which Robin rightly rejected. Typed `Claim` / `Evidence` models fixed it.
- **Codex asks approval for MCP tool calls through `mcpServer/elicitation/request`.** The first run refused that request type, and Codex reported "user rejected MCP tool call". Two changes:
  - Robin's tool now declares the MCP annotation `readOnlyHint: true`, so Codex runs it without asking.
  - The runtime host routes any elicitation to Robin's approval queue instead of refusing it, because the user's other MCP servers can elicit too.
- **Only a completed tool call counts.** A failed or rejected `mcpToolCall` is not a submission.
- **Your own MCP servers start in Robin-launched threads too:** `node_repl`, `cua_repl`, `codex_apps`, `postman` and `chatcut`. The last two failed to start because they need `codex mcp login`. That's harmless, but it slows startup and shows up as noise.

## Spike bug fixed along the way

The supervisor hung on shutdown under Python 3.12.0: `Server.wait_closed()` waited on a client connection that was never closed. The supervisor now tracks every connection, closes them on shutdown, and caps the wait at 5 s. It exits within a second.

## Still to test

- Inherited default policy and sandbox with no override
- Terminal **attach** (joining a live session) through Codex's own daemon; handoff already works
