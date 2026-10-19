# Continue a session in another runtime: build plan

Branch `feat/cross-runtime-resume`. Approach A: write the converted session into the target runtime's store, index it as an observed session, then reuse `takeover()`. Formats and live evidence: `docs/cross-runtime-resume.md`.

## Scope

- **In:**
  - All six directions between Claude, Codex and Pi.
  - `robin continue <session> --in claude|codex|pi`, and a "Continue in…" dialog in the session detail.
  - A preview with an editable first message, defaulting to "Continue from where the previous session left off."
  - The source is left untouched. Continued-from / continued-in links are shown on both sessions.
  - Refused while the source has a turn running, and when the session is too large for the target's context window.
- **Out:** compaction of oversized sessions; resuming without a first message.

## Corrections to `docs/cross-runtime-resume.md` (fixed in step 13)

- **Claude folder name:** the realpath with every non-alphanumeric character replaced by `-`. Robin has no resolver for this yet.
- **Pi file name:** `--<realpath, / replaced by ->--/<ISO-ts, : replaced by ->_<uuidv7>.jsonl`. The header `id` is a uuidv7.
- **Pi context window:** `~/.pi/agent/models-store.json` (`models[].contextWindow`). The default model is in `~/.pi/agent/settings.json`.
- **Codex context window:** `model_context_window` in `~/.codex/config.toml` if set, otherwise `~/.codex/models_cache.json` (`models[].slug`, `context_window`).
- **Claude `version` field:** omitted. The host runs the SDK's bundled CLI (2.1.300), not the installed one.
- **Package name:** `src/robin/continuation/`. The name "handoff" is already taken by the release-to-terminal action.
- **UUIDv7:** Python 3.13 has no `uuid.uuid7`, so a small helper is needed.

## Steps

| # | Step | Files | Outcome |
|---|---|---|---|
| 1 | **Live spike (gate).** For each target, write a minimal converted fixture (codeword, tagged tool call and result, thinking as text) and resume it through the supervisor with `ROBIN_RESUME_NATIVE_ID`, the way `supervisor/test_continuity.py:106-117` does. Variants: Codex with plain `response_item`s vs `task_started`/`task_complete` wrapping; Claude with and without `version`; Pi resumed by id, honouring `ROBIN_PI_SESSION_DIR`. | `supervisor/test_continue_live.py` | Codex `thread/resume`, Pi `--session <id>` and Claude SDK `resume=` each answer with the codeword. **Stop and adjust that writer if any of them fails.** |
| 2 | Canonical model and readers, compaction-aware | `src/robin/continuation/{__init__,canonical,readers}.py` (bodysnatcher MIT notice in `__init__`) | `read(runtime, path) -> Transcript(msgs, dropped, compaction, cwd, model)` |
| 3 | Writers, paths, ids | `continuation/{writers,paths,ids}.py` | `render(msgs, target, cwd) -> (native_id, final_path, bytes)`. Roots come from `HISTORY[rt].default_root()`; Pi uses `ROBIN_PI_SESSION_DIR` first. The Claude folder name comes from `ExecutionCwd(path).path`. `locate()` finds a managed source's transcript. |
| 4 | Size and context window | `continuation/size.py` | tokens ≈ bytes/4; refused above 60% of the window. Window defaults: 272k for Codex, 200k otherwise; 1M for Claude `[1m]` models. |
| 5 | `session_link` model and migration 0016 | `store/models.py`, `store/migrations/versions/0016_session_link.py` | The migration parity test passes |
| 6 | `INGEST_LOCK` around `_ingest_session`; `_catch_up` takes it too | `discovery.py:62-113`, `manager.py:587-594` | The watcher, scans, takeover catch-up and continue never tail one file concurrently. This also fixes an existing takeover/watcher race. |
| 7 | Narrow quiet-window exemption | `manager.py:566-574` | Skip a checkpoint path only if it equals the link's `written_path` **and** its size and mtime_ns are unchanged since Robin wrote it. `holders()` still runs. |
| 8 | Flow: `preview()` and `continue_()` | `continuation/flow.py`; cwd validation pulled out of `app.py:526-535` | See "Flow" below |
| 9 | API routes; `continued_from` / `continued_in` in `session_view` | `service/routes_continuation.py`, `app.py:130-239` | See "API" below |
| 10 | CLI `robin continue` | `cli.py`, modelled on `cmd_context` (`:539-584`) | See "CLI" below |
| 11 | Dashboard | `web/src/api.ts`, new `ContinueDialog.tsx`, `SessionDetail.tsx` | See "Dashboard" below |
| 12 | Tests | See "Tests" below | `uv run pytest` and the web tests pass |
| 13 | Docs | `cross-runtime-resume.md`, `capability-matrix.md` | The docs match what was built |

## Flow

**`preview(source, target)`** has no side effects.
- **Refusal codes:**
  - `same_runtime`;
  - `unsupported`, when the target lacks the takeover capability;
  - `no_cwd`;
  - `no_transcript`;
  - `source_busy`: a managed source is busy when its `runtime_state` is running or waiting_for_input (plus `holders()` if a terminal owns it). An observed source is busy when `holders()` finds a process or `_recently_written` is true;
  - `too_large`.
- **`preview_hash`:** sha256 of the source id, path, size, mtime_ns, target runtime, model and window, and converter version. The rendered bytes are a deterministic function of these, so they aren't hashed separately; a converter change bumps `CONVERTER_VERSION`. The first message is not part of the hash.

**`continue_(…, text, preview_hash, action_id)`:**
1. A repeated `action_id` returns the stored result.
2. Run the preview again. A refusal raises. A changed hash returns `409 preview_stale`.
3. Take a per-source lock.
4. Write a temporary file `.<name>.tmp-<rand>` in the target folder. No history glob matches that name. Fsync it and stat it.
5. Under `INGEST_LOCK`:
   1. One transaction inserts:
      - the observed `session` row (new native id, the checked cwd, and `project_id`, `current_task_id`, `skip_permissions` and `independence` copied from the source);
      - a `TaskSession` row;
      - the `session_link` row;
      - an `AuditEntry` with `session.continue`.
   2. `os.replace` the temporary file into place.
   3. `_ingest_session` on the new file.
6. Call `manager.takeover(target, text, action_id)`.

**Inheritance:** `_relaunch_env` already turns those columns into the resume id, findings and credential (when the session has a task), the reviewer deny rule, and skip-permissions. If the target runtime can't skip permission prompts, that flag is cleared and the preview shows a warning.

**Rollback:**

| Failure | State afterwards |
|---|---|
| Refused or stale | Nothing changed |
| Write fails | Temporary file removed |
| Rename fails | Rows deleted, temporary file removed |
| Index fails | Observed and linked; picked up by the next scan or by catch-up |
| Takeover rejected, or host fails to start | `_give_back` leaves the session observed, linked and continuable again |

## Format rules

**Readers** keep the source runtime's latest compaction summary plus the messages it kept.
- **Claude:**
  - skip sidechain, `isMeta` and system-reminder/notice text;
  - merge records that share `message.id`;
  - images become `[image]`.
- **Codex:**
  - read `response_item`s only;
  - skip developer messages and injected `<environment_context>` / `<user_instructions>` / `# AGENTS.md` user messages;
  - `agent_message` becomes assistant text;
  - `function_call` and `custom_tool_call` (raw `input`) become ToolCall;
  - both output types become ToolResult;
  - `compacted` uses `replacement_history`.
- **Pi:** user / assistant / toolResult messages; `compaction` uses `summary` and `firstKeptEntryId`.
- **Every reader** counts what it drops, including an `unknown_items` bucket, so format drift stays visible.

**Writers.** Every direction uses:
- `[external_agent_tool_call: Name]\ninput: {json}` for tool calls;
- `[external_agent_tool_result]…[/external_agent_tool_result]` for tool results;
- `[external_agent_reasoning]…[/external_agent_reasoning]` for foreign thinking.

Tool messages become user-role text, roles are merged so they alternate, and timestamps come from the source.
- **Claude:** uuid4 id; `<root>/<slug>/<id>.jsonl`; text-only records chained by `parentUuid`; `userType: "external"`; no `version`.
- **Codex:** uuidv7 id; `<root>/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`; `session_meta`, then `response_item` messages only. Wrapping is added only if the spike shows it is needed; writing `event_msg` copies would make Robin's indexer count every message twice.
- **Pi:** uuidv7 id; header `{type: "session", version: 3, id, timestamp, cwd}`; messages with 8-hex ids chained by `parentId`; zeroed `usage`. The spike confirms which other assistant fields Pi requires.

## API

`POST /api/v1/sessions/{id}/continue/preview`, body `{"runtime": "codex"}`, returns `200`. A refusal is still a `200` with `refusal` set.

```json
{"source": {"session_id": "…", "runtime": "claude", "native_session_id": "…", "mode": "observed", "cwd": "…"},
 "target": {"runtime": "codex", "model": "…", "context_window": 272000, "window_source": "models_cache.json"},
 "messages": {"total": 184, "user": 41, "assistant": 77, "tool_calls": 33, "tool_results": 33},
 "dropped": {"images": 2, "sidechain_records": 17, "system_reminders": 9, "before_compaction": 120,
             "unknown_items": 0, "thinking_as_text": 12, "tool_calls_as_text": 33},
 "compaction": {"source_summary_used": true},
 "estimate": {"bytes": 412000, "tokens": 103000, "limit_tokens": 163200, "fits": true},
 "inherits": {"task_id": "…", "project_id": "…", "skip_permissions": true, "independence": null},
 "warnings": [], "refusal": null,
 "default_text": "Continue from where the previous session left off.",
 "preview_hash": "sha256:…"}
```

`POST /api/v1/sessions/{id}/continue`, body `{"runtime", "text", "preview_hash", "action_id"}`, returns `202` `{"action", "session_id", "link"}`. Errors:
- `409`: `preview_stale` / `source_busy` / `action_id_conflict` / takeover's own codes;
- `422`: `too_large` / `same_runtime` / `unsupported` / `invalid_request`;
- `404`: unknown session.

Session view: `continued_from: {session_id, runtime, at} | null` and `continued_in: [{session_id, runtime, at}]`.

## `session_link` (migration 0016)

`link_id` PK, `kind` (`'continued'`), `source_session_id` FK (indexed), `target_session_id` FK unique, `source_runtime`, `target_runtime`, `source_path`, `written_path`, `written_size`, `written_mtime_ns`, `preview_hash`, `manifest` (JSON), `action_id` unique (no FK, because the action is created after this row), `created_at`. Constraint names follow `NAMING`, with raw statements like 0015 and no downgrade.

## Tests

- **Fixtures:** sanitised real-shape files for each runtime, covering compaction, sidechains, images, Codex custom tool calls / agent_message / injected AGENTS.md, and Pi compaction.
- **Readers and writers:** round trips in all six directions. Assert:
  - the tagged text, thinking as text and alternating roles;
  - Pi's zeroed usage, Codex's uuidv7 and Claude's `parentUuid` chain;
  - folder names for paths containing `_`, `.` and spaces, and `/private` realpaths.
- **Flow** (fake runtime, as in `tests/test_takeover.py`):
  - hash drift is refused and writes no file;
  - busy managed and observed sources are refused;
  - an oversized session is refused;
  - the happy path: managed, indexed, linked, audited, inheritance visible in `session_started`;
  - the exemption is lost once the file is touched;
  - host failure gives the session back, and it can be continued again;
  - a concurrent watcher creates no duplicate row or events;
  - a repeated `action_id` writes no second file.
- **Service, CLI, migration parity,** and `web/src/__tests__/continue.test.tsx` for each dialog state.
- **`supervisor/test_continue_live.py`** stays as the live regression test for each runtime pair.

## Risks that remain

1. Resume can break when a runtime upgrades. The spike only proves the versions installed today; the bundled Claude CLI and the installed one can drift apart.
2. If a runtime resumes under a new id, the preset id is kept and the forked file later appears as a separate observed session.
3. If the runtime touches the file during a failed start, the retry waits out the 120-second quiet window.
4. `INGEST_LOCK` only covers this process.
5. Context-window lookups are best effort, and bytes/4 is a rough estimate, so a borderline session can still overflow.
6. A session that was written but failed to start stays in the target runtime's own resume list.
