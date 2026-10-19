# Cross-runtime resume: how bodysnatcher does it

Reverse-engineered from [alfredosdpiii/bodysnatcher](https://github.com/alfredosdpiii/bodysnatcher) v0.3.0 (MIT, Rust, ~3,900 lines; read 2026-10-17), and checked against real sessions on this machine: Claude Code 2.1.302 and Codex 0.163.0. Only Claude, Codex and Pi are covered here, because those are the runtimes Robin supports. bodysnatcher also handles Factory Droid and OMP.

MIT lets Robin port the code. Robin must keep bodysnatcher's copyright notice when doing so, for example in a `NOTICE` file or in the module docstring.

## The whole trick

1. Parse the source session into a canonical message list.
2. If the source runtime already compacted the session, keep only its latest summary and the messages it kept.
3. If the result is still too large for the target model, fold older turns into checkpoint messages.
4. Write a new JSONL file in the target runtime's own format, directly into its session store. The source file is never modified.
5. Run the target's native resume command, which loads that file as if the target had written it:

| Target | Command |
|---|---|
| Claude | `claude --resume <id>` |
| Codex | `codex resume <id>` |
| Pi | `pi --session <path>` |

No API calls and no model are involved. It is pure format conversion.

## Canonical model

```
Msg   { role: User | Assistant | Tool, blocks: [Block], ts: str? }
Block = Text(str)
      | Thinking(str)
      | ToolCall   { id, name, args: json }
      | ToolResult { call_id, name?, content: str, is_error }
```

Tool results always become their own `Tool` message. This matches Pi's shape. Claude stores tool results inside user messages, so readers split them out and writers group them back.

## Reading

### Claude: `~/.claude/projects/<slug>/<uuid>.jsonl`
- A record is a conversation record when it has `message.role` and `isSidechain` is not true. Subagent sidechains are dropped.
- Assistant `message.content` blocks map as follows:
  - `text` becomes Text.
  - `thinking` becomes Thinking, skipped when empty.
  - `tool_use {id, name, input}` becomes ToolCall.
  - `image` becomes the text `[image]`.
- User content:
  - Each `tool_result {tool_use_id, content, is_error}` becomes a separate Tool message.
  - The remaining blocks are flattened into text.
  - Text that is empty or starts with `<system-reminder>` or `<system-notice>` is dropped.
- Compaction:
  - A record with `subtype: "compact_boundary"` marks a cut. Kept history resumes at `compactMetadata.preservedMessages.uuids[0]` (or `preservedSegment.headUuid`), looked up by record `uuid`.
  - The record with `isCompactSummary: true` that follows the boundary becomes the summary.

### Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`
- `session_meta.payload` holds `id` (or `session_id`) and `cwd`. `turn_context.payload.model` holds the model.
- Only `response_item` records are read:
  - `message` with role `user`: the `input_text` parts.
  - `message` with role `assistant`: the `output_text` parts.
  - `reasoning`: `summary[].text` becomes Thinking.
  - `function_call {call_id, name, arguments}` becomes ToolCall. `arguments` is a JSON string, which is decoded.
  - `function_call_output {call_id, output}` becomes ToolResult.
- Compaction: a `compacted` record whose `payload.replacement_history` (a list of response items) replaces everything before it. It falls back to `payload.message` as a summary.

### Pi: `~/.pi/agent/sessions/--<cwd, / replaced by ->--/<ISO-ts, : and . replaced by ->_<uuidv7>.jsonl`
- The `session` header holds `id`, `cwd` and `title`. `model_change` holds the model.
- `message` records with these roles are read:
  - `user`: flattened text.
  - `assistant`: `toolCall {id, name, arguments}` and `thinking` blocks.
  - `toolResult`: `toolCallId`, `toolName`, `content` and `isError`.
- Compaction: a `compaction` record with `summary` and `firstKeptEntryId`.

### Compaction stitching (all runtimes)
- The result is the summary followed by the kept messages.
- Leading Tool messages whose call was compacted away are dropped.
- If the summary (a user message) would sit next to another user message, an assistant line, "Understood. Continuing from the summary.", is inserted between them.

## Writing

All writers give the new session a fresh id and copy the source `cwd`. If the source has no timestamps, they use the source file's mtime.

### Claude
- Path: `~/.claude/projects/<cwd with / and _ replaced by ->/<uuid4>.jsonl`.
- One record per Msg, chained by `parentUuid`:
  ```json
  {"type": "user|assistant", "message": {...}, "uuid": "...", "parentUuid": "<previous or null>",
   "timestamp": "...", "sessionId": "<new id>", "cwd": "...", "isSidechain": false,
   "userType": "external", "version": "2.1.220"}
  ```
- Assistant content uses `text`, `thinking` and `tool_use {id, name, input}`. The source model is copied into `message.model`.
- Consecutive Tool messages are grouped into one user record of `tool_result` blocks.

### Codex
- Path: `~/.codex/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<uuid4>.jsonl`.
- First a `session_meta` record (`ordinal` 0) with `id`, `session_id`, `cwd`, `cli_version`, `source: "cli"`, `originator` and `model_provider`.
- Then one `response_item` per block, with `ordinal` increasing:
  - User text becomes a `message` with `input_text`.
  - Assistant text becomes a `message` with `output_text`.
  - Thinking becomes `reasoning` with `summary: [{type: "summary_text"}]` and `content: null`.
  - ToolCall becomes `function_call` with `arguments` as a JSON string.
  - ToolResult becomes `function_call_output`.
- No `event_msg`, `turn_context` or token records are written.

### Pi
- Path (bodysnatcher): `~/.pi/agent/sessions/--<cwd-slug>--/<now_ms>_<hex32>.jsonl`. Pi itself writes `<ISO-ts>_<uuidv7>.jsonl`, keeps `_` in the folder name, and puts files flat in `--session-dir` when one is given; Robin follows Pi.
- Header: `{"type": "session", "version": 3, "id": ..., "cwd": ..., "title": ..., "titleSource": "user"}`.
- `message` records with 8-hex `id`s, chained by `parentId`.
- Assistant messages must carry a zeroed `usage` object (`input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, `cost{...}`), because Pi's renderer reads it without checking.
- A `toolResult` needs `toolName`. When the source does not record it (Claude and Codex results carry only the call id), the writer takes it from a call-id → name map built from the ToolCalls.

## Import-time compaction (`compact.rs`)

This only runs when the converted session would not fit the target model.
- Size estimate: about 4 bytes per token.
- Target size: 200k tokens, capped at 60% of the target's context window. 32k tokens are reserved for the runtime's system prompt and tools.
- The newest turns are kept word for word. The cut point is moved so no tool result is separated from its call, and the final turn is always kept.
- Older history is split into chunks of about 150k tokens, at most 12, never between a call and its result. Each chunk becomes a deterministic checkpoint: user requests, assistant outcomes, tools used, files touched and errors. Oversized blocks keep their start and end. The level of detail shrinks until the whole session fits.
- The context window is read from the target's config:
  - Codex: `model_context_window` or its models cache.
  - Pi: `models.json`.
  - Claude: 1M if the model is a `[1m]` model, or if any recent turn used more than 200k tokens.
  - Otherwise 272k for Codex and 200k for the others.

## What a Robin port must fix

These were found by comparing bodysnatcher with real files on this machine.

1. **Codex indexes threads in SQLite, but a rollout file alone is enough (verified 2026-10-17, Codex 0.163.0).** `~/.codex/state_5.sqlite` holds a `threads` table (all 333 rows `history_mode = paginated`), and `thread_history_1.sqlite` holds turns and items. Test setup: a throwaway `CODEX_HOME` with copies of both databases, plus a bodysnatcher-style rollout that was not in them. Result:
   - `codex exec resume <id>` found the rollout, loaded it and called the model. The call returned 401, as expected without a login.
   - For comparison, an unknown id fails at once with `no rollout found for thread id …`.
   - On resume, Codex inserted the thread into `threads` itself (`history_mode = legacy`, `source = cli`) and appended its usual records to the rollout.

   So Robin writes only the rollout file and never touches Codex's SQLite.

   **Live check with real logins (2026-10-17).** Synthetic converted sessions were written into the real stores, each holding a codeword plus one tool call and its result. Each was resumed with "Reply with only the codeword I gave you and the file you found":
   - Codex (importer-style rollout, tool call as tagged text): `codex exec resume <id>` answered `PELICAN notes.txt`.
   - Claude (bodysnatcher-style records, `version: "2.1.220"`, a `thinking` block with the placeholder signature, a native `tool_use` named `exec_command` with its `tool_result`): `claude -p --model claude-haiku-5-5 --resume <id>` answered `OSPREY — notes.txt`.

   Both models received the imported history.
2. **The Codex reader loses items.** Real rollouts also contain `custom_tool_call` / `custom_tool_call_output` (for example `apply_patch`) and `response_item: agent_message`, and bodysnatcher drops them. In a Codex → Claude conversion, every file edit Codex made would disappear.
2a. **Codex's own Claude importer flattens tool calls (observed in 50 sessions it imported here).** `~/.codex/external_agent_session_imports.json` maps each `~/.claude/projects/…jsonl` it imported, with a `content_sha256`, to an `imported_thread_id`. The rollouts it writes contain:
   - only `message` items: `input_text` for the user, `output_text` for the assistant;
   - tool calls rendered as text: `[external_agent_tool_call: <Name>]\ninput: {…}` and `[external_agent_tool_result]…[/external_agent_tool_result]`;
   - each turn wrapped in `event_msg` `task_started` → `item_completed` per item → `task_complete`.

   This avoids replaying `function_call`s to tools Codex doesn't have, such as `Read` or `Skill`. Robin's Claude → Codex writer should copy this shape rather than bodysnatcher's native `function_call` items. The same reasoning applies to Codex → Claude: tool calls become tagged text, not `tool_use` blocks with Codex tool names.
3. **Claude thinking signatures.** Real Claude thinking blocks carry an API `signature`. When converting from Codex or Pi, bodysnatcher writes the placeholder `"signature": "reasoning_content"`. In the live check, a session with a placeholder signature resumed without error (Haiku, Claude Code 2.1.302). That was one model and one version, and the API doesn't guarantee it. The safe choice when writing to Claude is still to turn foreign thinking into a text block or drop it.
4. **Claude records are thinner than the real ones.** Real assistant records split one API message across several records with the same `message.id`, and carry `requestId`, `gitBranch`, `entrypoint` and more. bodysnatcher also hardcodes `"version": "2.1.220"` (installed: 2.1.302). Resume probably tolerates the thinner records, but test against the installed version instead of hardcoding.
5. **Claude folder names.** bodysnatcher only replaces `/` and `_`. Claude replaces every non-alphanumeric character of the folder's real path with `-` (and shortens names over 200 characters with a hash, which Robin refuses rather than guesses). Robin had no resolver; `continuation/paths.py` is it.
6. **Codex ids.** Real Codex ids are UUIDv7 (`01a1…`), and bodysnatcher writes v4. They probably still work, but generate v7 to match.
7. **Images and subagent sidechains** become the text `[image]` or are dropped. Accept this and say so in the preview.

## Robin's implementation

Built on `feat/cross-runtime-resume`; plan and decisions in `docs/design/cross-runtime-continue-plan.md`.

- `src/robin/continuation/`: `readers.py` (compaction-aware, counts what it drops), `canonical.py` (tagged-text turns), `writers.py`, `paths.py`, `size.py` (60% of the target's context window), `flow.py` (preview, continue, links).
- Every direction writes text messages only: tool calls and results as `[external_agent_tool_call: …]` / `[external_agent_tool_result]`, reasoning as `[external_agent_reasoning]`, all under the assistant role as in Codex's importer.
- The new session is recorded (observed, with a `session_link` row) before its file appears in the store, under the same lock the watcher takes, then handed to `takeover()`. Takeover's quiet window skips only the file Robin wrote, while its size and mtime are unchanged.
- `supervisor/test_continue_live.py` resumes Robin-written sessions through each production host (Claude SDK `resume=`, Codex app-server `thread/resume`, Pi `--session <id>`); all passed 2026-10-18.
