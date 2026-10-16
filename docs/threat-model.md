# Threat model (WP-4.5)

Covers TR-070 and NFR-08. Robin runs on one machine for one owner and listens only on loopback. This page
records what it defends against, where each defence is in the code, and the test that keeps it in place.

## Assets

- The owner's coding-agent sessions and the power to steer them: messages, interrupts, approvals, handoff.
- Credentials: the CLI token (`cli.token`), the browser session cookie and CSRF token, each managed session's
  agent credential (`robin_agent_…`), and any secrets that appear in transcripts.
- The owner's files: project folders, Robin's data directory, and anything else the owner's account can
  reach.
- Findings, syntheses and context packets, and the record of who wrote which.

## Who attacks, and how

| Actor | Can do | Can't do |
|---|---|---|
| A web page open in the owner's browser | Send requests to `127.0.0.1`, try DNS rebinding | Read responses, read cookies |
| A malicious repository, or an agent prompt-injected by one | Write any text into transcripts, findings and tool output; write files the agent can reach | Call the user API without a credential |
| A managed session's tools | Call `/agent/*` with that session's own credential | — |
| A forged backup or export folder the owner is persuaded to restore | Supply any database contents, and a manifest that matches them | — |

Out of scope: another local user (the data directory is `0700`), root, and an unsandboxed agent that reads
`cli.token` straight from the data directory. Only independent reviewers run with a rule that denies reading
it (`runtime/manager.py`); that limit is also listed in the release record.

## Defences

| Threat | Defence | Code | Test |
|---|---|---|---|
| Cross-origin requests, DNS rebinding | Exact Host and Origin match; cookie session with CSRF header; single-use bootstrap secret that expires in 60 s and is rate-limited | `service/auth.py` | `test_service.py::test_rejects_foreign_host_origin_and_anonymous`, `…bootstrap…`, `…csrf…` |
| Hostile Markdown/HTML in agent or task text | React renders text nodes only: no `dangerouslySetInnerHTML`, no Markdown renderer, no data-built `href`/`src`. CSP `script-src 'self'`, `frame-ancestors 'none'` | `web/src`, `service/app.py` | `components.test.tsx` (feed), `hostile.test.tsx` (task, finding, synthesis), `test_service.py::test_security_headers` |
| Path traversal and symlink escape from task files and finding evidence | Resolve symlinks, then require the path to stay under the project root; reject NUL bytes | `workspace/tasks.py` `_snapshot`, `read_project_file` | `test_workspace.py`, `test_compare.py` |
| An agent running inside Robin's own data | Projects and launch folders may not contain, or sit inside, the data directory | `workspace/tasks.py` `_not_overlapping`, `service/app.py` launch | `test_workspace.py::test_a_project_never_overlaps_robins_data_directory`, `test_service.py::test_validation_and_error_codes` |
| A forged backup that names paths outside the store (pruning deletes them, export writes there) | Blob paths are built only from 64-hex hashes. Restore checks every blob reference and every id that export uses as a file name before anything is written. Export checks that each folder stays inside its output | `store/blobs.py`, `backup.py` | `test_backup.py::test_a_forged_backup_is_refused_and_leaves_nothing`, `…blob_paths_are_only_ever_hashes` |
| Process-argument injection | Every process starts from an argument list; nothing uses `shell=True` or tmux. Prompts go to the host as JSON on stdin. Native session ids must start with a letter or digit, so an id read from a transcript can never be taken as a flag (`claude --resume --dangerously-…`). The Terminal.app line is `shlex`-quoted, then AppleScript-escaped | `runtime/terminal.py`, `supervisor/`, `hooks/install.py` | `test_handoff.py::test_a_native_id_that_could_be_a_flag_never_becomes_a_command` |
| MCP scope escape | Each credential identifies one session. A session reads only its own work and exact granted versions and ranges, and publishes only to its own task. The credential is deleted when its host exits | `retrieval.py`, `runtime/manager.py` | `test_retrieval.py::test_task_sessions_get_tools_and_the_credential_is_scoped`, `…credential_dies_with_its_host` |
| Credentials leaking through shared excerpts, logs or diagnostics | Secrets are masked at index time and in logs and diagnostics with gitleaks rules plus Robin's own credential rule. Backups and exports leave out `cli.token`, `auth.json` and `service.json` | `redact/scanner.py`, `logs.py`, `diagnostics.py`, `backup.py` | `test_redact.py::test_robins_own_agent_credentials_are_redacted`, `test_operations.py::test_diagnostics_preview_first_and_hold_no_secrets`, `test_backup.py::test_export_is_readable_relative_and_credential_free` |
| Notification text running as AppleScript | The title and body reach `osascript` only as `argv`, never inside the script; notifications carry runtime, session and task names, never prompt or message text | `notifications.py` | `test_notifications.py::test_text_reaches_osascript_only_as_arguments`, `…without_message_text` |
| An agent fed its own output as independent confirmation | Lineage is walked for every packet source and grant; the recipient's own work, or work built on it, is left out unless the user includes it explicitly | `context/lineage.py`, `context/packets.py`, `retrieval.py` | `test_context.py::test_a_recipients_own_output_is_left_out_unless_included`, `test_retrieval.py::test_a_grant_of_a_sessions_own_work_back_needs_an_explicit_allow` |
| Worktree removal destroying work | Removal is a separate confirmed action: it shows uncommitted files and unmerged commits, refuses while the session is connected, discards uncommitted files only when told to, never deletes the branch, and is audited. Worktrees never sit inside the data directory or the repository | `workspace/worktrees.py` | `test_worktrees.py` |
| Secrets in what the Map shows (titles, tool targets, todos, file paths) | Each string is secret-scanned before it is truncated and stored, at derivation time; the table is rebuilt from the events when the redaction rules or the extractor change. The Map reads only that table, never raw payloads | `activity.py` | `test_activity.py::test_secrets_never_reach_the_table`, `…rebuilt_when_rules_change` |
| Robin contending with an agent for Git's index lock | Git context is read with `GIT_OPTIONAL_LOCKS=0`, off the request path | `gitctx.py` | `test_rungit.py` |

## Accessibility (NFR-08)

| Requirement | How | Test |
|---|---|---|
| Keyboard-only use of the full scenario | Every control is a native element. On the Map, every card is a button in the Tab order and focusing one pans it into view; Esc closes the detail drawer and returns focus to the card; `+`, `-` and `0` zoom. The session list is one Tab stop: arrow keys, Page Up/Down, Home and End reach every session, and the focused row is always rendered. Closing a panel returns focus to whatever opened it. Opening a session moves focus to its heading | `components.test.tsx`, `tests/e2e/test_web.py::test_keyboard_only_reaches_every_session_and_keeps_focus` |
| Contrast | Text tokens are at least 4.5:1 and focus rings and field borders at least 3:1, in light and dark themes | `contrast.test.ts`; axe in the e2e test when the network is available |
| State not shown by colour alone | Every state has a text label (`StateBadge`, Map state chips, status pills, evidence kinds); reasons a control is disabled are shown as text | `components.test.tsx` |
| Announced state changes | Status regions for session state, attention count, search result count and action results | `components.test.tsx::a session state change is announced` |
| Reduced motion | The global `prefers-reduced-motion` rule turns off the Map's spinners and pan easing | — |

## Known limits

- An agent that isn't sandboxed runs as the owner and can read `cli.token`. Robin can't prevent this. Only
  independent reviewers get the deny-read rule.
- `pgrep -f` on a native id is a heuristic for "a terminal still holds this session", and may over- or
  under-report.
- The backup database holds prompts and transcripts by design. Treat a backup folder like the data directory.
- The CLI token has no recognisable prefix, so the scanner can't catch it if it shows up in a transcript.
