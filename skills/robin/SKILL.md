---
name: robin
description: Drive Robin (the local coding-agent hub) from this chat — list and search Claude, Codex and Pi sessions, continue a session in another runtime, see and answer what's waiting (questions, approvals), message, interrupt or stop a session, start a new one, or hand a session to this terminal. Use for /robin, or whenever the user mentions Robin or asks to continue/move a session to Claude, Codex or Pi, "what's waiting on me", "my agent sessions", or to start an agent session.
---

# Robin

Robin runs as a local service; the `robin` CLI talks to it over HTTP. Everything here is a `robin …` shell command. Never read or write Robin's database or the agents' session files directly.

## Before the first command

1. `command -v robin` — if missing: "robin isn't on PATH; install it with `uv tool install --editable <robin repo>`" and stop.
2. Any command failing with "Robin isn't running" → run `robin serve --no-browser` in the background, wait a few seconds, retry once.

## Sessions are named by ID prefix

`continue`, `chat` and lookups accept any unique start of a session ID (e.g. `3fa2`). `open`, `message`, `interrupt`, `stop` and `reclaim` need the full ID — resolve it first from `robin sessions --json`.

When the user names a session by description ("the login bug one", "my last Codex session here"), find it:
- `robin sessions --json` — fields: `session_id`, `runtime`, `title`, `runtime_state`, `mode` (`observed` = read-only in Robin, `managed` = Robin controls it), `execution_cwd`, `branch`, `last_activity_at`, `continued_from`, `continued_in`, `controls.<action>.enabled/reason`. Filter with `jq` (by runtime, cwd = current folder, title) rather than dumping it.
- `robin search "<words>" --kind session` (or without `--kind` to search messages too).

If more than one session matches, list the candidates and ask. Show sessions as a short table: short ID (8 chars), runtime, state, folder (basename), title, last activity.

## Commands

| User wants | Command |
|---|---|
| list sessions | `robin sessions --json` → table (default: 15 most recent; filter to current folder if they say "here") |
| find a session / text | `robin search "<query>" [--runtime claude\|codex\|pi] [--kind session\|event\|task\|finding]` |
| what's waiting | `robin attention` |
| approve / deny | `robin approve <attention-id>` / `robin deny <attention-id>` |
| answer a question | `robin answer <attention-id> <answer per question…>` (option number or text; `--decline` to refuse). Works for terminal Claude sessions too while their question hook waits |
| send an instruction | `robin message <session> --text "…"` |
| interrupt / stop | `robin interrupt <session>` / `robin stop <session>` |
| start a session | `robin run claude\|codex\|pi [--cwd DIR] [--worktree] [--skip-permissions] --prompt "…"` |
| take back a handed-off session | `robin reclaim <session>` |
| a session's runs and changed files | `robin runs <session>` |
| health check | `robin doctor` |

`message`, `interrupt`, `stop` and approvals only work on managed sessions; if `controls.<action>.enabled` is false, tell the user the `reason` instead of trying.

## Continue in another runtime

Two steps, always:
1. `robin continue <session> --in claude|codex|pi` (add `--text "…"` if the user gave a first message). In this shell it only previews: what carries over, what's dropped, whether it fits, and a `--confirm sha256:…` command. Nothing is written yet. Summarize the preview for the user and ask to go ahead.
2. On yes: run the printed command with `--no-chat` added. Report the new session ID and its native ID.

If the preview is refused (e.g. a terminal still holds the session), relay the reason and stop.

## Open a session in a terminal

This chat can't become another session. To work in one with its own UI:
- Observed session (started in a terminal): give the native resume command — `claude --resume <native_session_id>`, `codex resume <native_session_id>`, `pi --session <native_session_id>` — run from its `execution_cwd`.
- Managed session: `robin open <full-id> --print-only` releases it from Robin and prints the command to run; tell the user it's handed off and `robin reclaim <id>` gives it back. Ask before doing this — it changes who controls the session. Add `--force` only if the user agrees to end a running turn.

## Confirm first

Ask before: `continue --confirm`, `approve`, `deny`, `answer`, `stop`, `interrupt`, `open` (handoff), `run --skip-permissions`. An explicit instruction naming the action and the session ("approve it", "stop 3fa2") is the confirmation.

## Output

Short. Tables for lists, one line per action result. Don't paste raw JSON.
