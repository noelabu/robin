# Pi RPC — Phase 0 findings

- **Runtime:** Pi via `pi --mode rpc` (strict JSONL over stdio), spawned by a per-session runtime host under the supervisor
- **Versions seen:** `@earendil-works/pi-coding-agent` 1.2.0 (Homebrew). Protocol docs ship with the package under `docs/rpc*.md` and `docs/json.md`.
- **Account:** Pi's own provider configuration. The default here is OpenRouter (`moonshotai/kimi-k3`, API key, `pi auth check`: ready). Usage is billed per token by OpenRouter.
- **Machine so far:** MacBook Pro M4 Pro (development). Re-run on the Mac mini M4 for the recorded results.
- **Evidence:** `supervisor/test_live.py pi`, 2026-10-15 (`evidence-20261015-103247.json`). Passed every check.

## Capability floor (PRD §16)

| Floor item | Status | Evidence |
|---|---|---|
| Launch with native ID capture | **Pass** | RPC mode emits no session header; `get_state` returns `sessionId` and `sessionFile` |
| Structured event streaming | **Pass** | `agent_start`, `turn_start`, `message_start/update/end`, `tool_execution_start/update/end`, `turn_end`, `agent_end`, `agent_settled`, queue and compaction events |
| Follow-up message | **Pass (new run)** | `prompt` after `agent_settled` returns `disposition: "started"`. During a run, `prompt` with `streamingBehavior: "steer"` (or `steer` / `follow_up`) queues input; not exercised yet. |
| Interruption | **Pass** | `abort` → `agent_settled` with `aborted: true` |
| Approval response | **Pass, through a Robin extension** | Pi has no built-in approvals: its security doc says it "does not ask for approval before every tool call". Robin loads `supervisor/pi_robin_approval.ts` (`pi -e`). Its `tool_call` handler calls `ctx.ui.confirm`, which RPC turns into an `extension_ui_request`. The runtime host answers with `extension_ui_response {confirmed}`. A `bash` call was approved this way after the service was SIGKILLed and reconnected. |
| Finding publication path | Not started | Candidates: a Robin extension tool (`pi.registerTool`) or MCP (`.pi/mcp.json`), or a parsed final-message block |
| Hosting under the supervisor | **Pass** | The host and its `pi` child survived a service SIGKILL while waiting on an approval. Events stayed contiguous, the host was spawned once, and nothing was left running. |

**Classification: meets the floor except finding publication, still to test. Approvals depend on Robin's extension (owner decision below).**

## Owner decision needed

**Should Robin-launched Pi sessions load Robin's approval extension by default?**

Pi's native behaviour is to run tools without asking. Loading the extension makes Robin-launched Pi sessions *stricter* than Pi's default, so it doesn't break FR-012 ("never more permissive than the harness default"). But it does mean Robin, not the harness, decides which calls need approval, which departs from the PRD principle that the harness owns permissions.

- **Recommended:** load it by default and label it in the capability report ("approvals: Robin extension, rule = not read-only and destructive or networked"), with a per-launch option to turn it off.
- **Alternative:** match Pi's native behaviour. Pi sessions then never raise approvals, and the capability report says "runtime does not ask for approval".

## Notes for the build

- **Framing.** Split stdout on LF only and strip an optional CR. Python's `readline()` does this. Node's `readline` would wrongly split on U+2028/U+2029.
- **Gated tools.** The extension uses the rule from Pi's extension docs: ask unless the tool is read-only, or is neither destructive nor networked. `read` runs without asking; `bash`, `write` and `edit` ask.
- **Dialogs.** `select`, `input`, `editor` and other `confirm` dialogs from the user's own extensions are answered `cancelled` by the spike and recorded as `pi_unhandled_dialog`. Robin should show them as attention items. Dialogs with a `timeout` auto-resolve on Pi's side; Robin's extension sets none.
- **User extensions load too.** `~/.pi/agent/extensions` holds three `orca-*.ts` extensions from the same third-party tool whose hooks run in Claude Code. They load in Robin-launched sessions as well.
- **Project trust.** RPC can't show Pi's trust prompt. The host passes `--no-approve`, so project-local `.pi/` resources are skipped unless a saved trust decision exists. That's the safe default. A Robin task that needs project extensions would pass `--approve` explicitly.
- **Sessions.** The test uses `--session-dir` in a temp folder to keep test sessions out of the user's Pi history. Real launches should use Pi's default location so `pi --session <id>` finds them.
- **Pi has no sandbox.** Its security doc says isolation must come from the OS, a container or a VM. So Robin can't enforce review independence for Pi through the runtime (TR-064); the capability report must say "not isolated".

## Still to test

- Native resume: `pi --session <id>` / `--continue` after the runtime host exits
- Steering and follow-up during a run
- Terminal mode: `pi --session <id>` in a terminal as handoff
- Finding publication through an extension tool, and with MCP disabled
- Hook equivalent for observed sessions (WP-2.6): Pi's mechanism is extensions in `~/.pi/agent/extensions`, not a hooks config file
