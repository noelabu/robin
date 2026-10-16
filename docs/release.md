# Release record: Robin 0.1.0 (WP-4.6)

- **Date:** 2026-10-16
- **Requirements:** TR-090–091, PRD §15 (acceptance scenario) and §20 (launch checklist)
- **Code:** `main` at `f91d964`, plus this record
- **Result:** the P0 acceptance scenario passes live on the reference machine. All three runtimes ship **live**.

## Reference machine

Owner decision 2026-10-16: the development MacBook is the reference machine. It replaces the Mac mini M4.

| | |
|---|---|
| Machine | MacBook Pro, Apple M4 Pro (`Mac16,7`): 14 cores (10 performance, 4 efficiency), 48 GB |
| OS | macOS 26.5 (25F71) |
| Python | 3.13.2, SQLite 3.53.4 with FTS5 |
| Tooling | uv 0.6.7, Node 22.23.2, tmux available |

## Runtimes

| Runtime | Level | Tested versions | Sign-in | Approvals | Review independence | Terminal | Finding paths |
|---|---|---|---|---|---|---|---|
| Claude Code | **live** | claude-agent-sdk 0.2.167 (bundled Claude Code 2.1.300) and 0.2.168; `claude` CLI 2.1.301 | the owner's `claude` login (subscription); Robin never reads the token | native | enforced (deny rules) | handoff | tool, message |
| Codex | **live** | codex-cli 0.162.1 | the owner's Codex login | native | enforced (deny rules) | handoff | tool, message |
| Pi | **live** | pi-coding-agent 1.2.0 | Pi's configured provider | Robin's extension (asks before `bash`, `write`, `edit` and unannotated tools) | **not isolated**: Pi has no sandbox, and the session is labelled so | handoff | tool, message |

Every runtime launches, resumes, takes new turns and queued follow-ups, interrupts and answers approvals. Codex can also steer a running turn; Claude Code and Pi queue a message sent mid-turn for the next turn. Sessions started outside Robin are seen through opt-in hooks (`robin hooks install`) or by following their transcripts. Observed sessions are read-only in Robin, and the dashboard offers the native resume command instead.

Capability details and the evidence for each one are in `docs/capability-matrix.md`.

## PRD §15 acceptance scenario

`tests/live/test_release.py` runs the scenario against Robin installed with `uv tool install` into a clean tool folder and started as `robin serve`, with a fresh data directory, using the owner's real Claude Code, Codex and Pi logins. It passed on 2026-10-16 in 81 s.

| Step | Result |
|---|---|
| Install, start the service, capability reports for all three runtimes | pass: all three `live`; `robin doctor` all OK |
| Index a historical session from each runtime; show source and read-only status | pass: one native session per runtime, started outside Robin, indexed with its native ID; controls disabled with "observed sessions are read-only in Robin" |
| Register a Git repository, create a PRD-review task, attach one Markdown PRD | pass |
| Launch a managed session per runtime; native IDs preserved; all under the task | pass |
| Observe activity, answer an approval, one attributed finding per agent, MCP off for one | pass: Claude Code published by message with Robin's tools off; Codex and Pi by tool; Pi's `bash` approval answered and acknowledged |
| Compare; select the Codex and Pi findings for Claude Code | pass |
| Preview and confirm a packet that leaves out unselected content | pass: the packet carries the objective and the two selected findings, not Claude Code's finding or an unshared planted finding |
| Deliver to Claude Code, request a synthesis, keep delivery status and the artifact | pass: delivery acknowledged; synthesis saved with lineage to both findings and the packet |
| Search a phrase from the synthesis, navigate to the task and source findings | pass |
| Restart the service while a session runs; reconciled without relaunch; links, findings, manifests, history and search persist | pass: a Codex turn was still running after the restart; no session was relaunched (one host start each); the turn was then interrupted and acknowledged |

### Also verify

| Check | Evidence |
|---|---|
| Interrupt a supported active run; an unsupported action is disabled with a native fallback | Live: release scenario (Codex after the restart) and `test_runtimes_live.py` for all three runtimes. Disabled controls carry a reason: `test_service.py`, release scenario (observed sessions) |
| Stale connection, unknown event, duplicate replay, failed delivery, expired approval; honest state; an ended turn is never task completion | `test_manager.py` (stale on supervisor loss), `test_supervisor_source.py` and `test_claude_translate.py` (unknown events and sessions quarantined, stream continues), `test_state.py` and `test_supervisor.py` (replay applies once), `test_context.py::test_a_failed_delivery_is_reported_and_never_shown_as_delivered`, `test_attention.py` (expired requests can't be answered), `test_workspace.py::test_an_ended_turn_never_completes_the_task` |
| Refresh or close the browser while work runs; reopening keeps the session, no relaunch or stop | `tests/e2e/test_web.py::test_refreshing_or_closing_the_browser_leaves_running_work_alone` (real browser) |
| Publish a finding while another reviewer runs; the independence boundary holds before and after an approved transfer | Live: release scenario and `test_phase3_exit.py` (the synthesizer can't read an unshared finding after the transfer); `test_independence_live.py` for Claude Code and Codex |
| Open the native terminal during an active turn and a pending approval; never two owners or a duplicate response | `test_handoff.py` (refused mid-turn unless confirmed; after handoff the pending request can't be answered by Robin), `test_service.py::test_handoff_and_reclaim_through_the_api`. Scripted runtime, not a live run |
| Attach a session to a second task; a new native session is the default; late events keep their task | `test_workspace.py::test_reattaching_needs_confirmation_and_binds_from_the_next_run` and `test_attaching_back_to_the_current_task_cancels_a_pending_switch` |
| Stop the supervisor; sessions disconnected; nothing relaunches; native resume offered | Live: release scenario (4 sessions; still disconnected after 10 s; resume enabled). `test_supervisor.py::test_supervisor_crash_ends_hosts_and_nothing_relaunches`, `test_manager.py::test_supervisor_gone_marks_disconnected_without_relaunch` |
| Back up and export a task, restore into a clean data directory; findings, manifests and hashes match | Live: release scenario (artifacts and packet hashes identical; every blob present). `test_backup.py`, including forged-backup refusal |

## Test results (2026-10-16)

| Suite | Command | Result |
|---|---|---|
| Python | `uv run pytest` | 281 passed, 25 skipped (the opt-in suites) |
| Web components | `cd web && npm test` | 32 passed |
| Browser (Playwright) | `ROBIN_E2E=1 uv run pytest tests/e2e` | 8 passed, including the axe accessibility check |
| Packaging | `ROBIN_PACKAGING=1 uv run pytest tests/packaging` | 1 passed: the wheel installs into a clean venv and runs |
| Live runtimes | `ROBIN_LIVE=1 uv run pytest tests/live` | release scenario passed; 14 of the other 15 passed in the suite run; the 15th (`test_pi_observer_fires`) timed out once and passed 6 of 6 re-runs (see Unresolved limits) |
| Lint and types | `ruff check`, `ruff format --check src tests`, `mypy` | clean |

## Recovery evidence

| Scenario | Evidence |
|---|---|
| Service restart with a turn running | Live, release scenario: no relaunch, same native sessions, the running turn interrupted afterwards. `test_supervisor.py::test_session_survives_service_loss_and_replays_exactly` |
| Service loss during a pending approval | Live, `test_runtimes_live.py` for all three: the approval answered after reconnecting |
| Supervisor stop or crash | Live, release scenario; `test_supervisor.py`, `test_manager.py` |
| Supervisor upgrade | `test_operations.py::test_supervisor_restart_refuses_running_turns_then_resumes_idle_sessions` |
| Crash during dispatch; unknown outcome | `test_crash.py`, `test_ledger.py::test_recover_marks_in_flight_outcome_unknown_and_late_ack_resolves` |
| Migration interruption, pre-migration backup | `test_migrations.py` |
| Backup and restore into a clean directory | Live, release scenario; `test_backup.py` |

## Benchmarks

`bench/dataset.py` (10,000 observed sessions, 200,000 events, seed 7; built in 146 s) and `bench/run.py`, run on the reference machine on 2026-10-16:

| Measure | Target | Result |
|---|---|---|
| Service startup to healthy | ≤ 2 s | **0.48 s** |
| Workspace snapshot (500 observed sessions + managed ones) | — | 120 ms, 763 KB |
| Search, 100 two-word queries | p95 ≤ 300 ms | p50 20.3 ms, **p95 21.4 ms** |
| Idle CPU, service + supervisor, 30 s | < 1% | **0.0%** |
| Live events, 20 concurrent sessions, 30 s | 95% within 2 s | **100%**; p50 239 ms, p95 286 ms (387 events/s) |
| Concurrent active sessions | 20 | 20 |

Method and earlier runs: `docs/benchmarks.md`.

## PRD §20 launch checklist

| Item | Where |
|---|---|
| Tested runtime versions, sign-in, OS, capability differences, live or observed-only level | This record; `docs/capability-matrix.md` |
| Anonymized transcript fixtures for every adapter | Partly: synthetic Claude Code, Codex and Pi transcript records built in `test_discovery.py`, `test_claude_translate.py` and `test_codex_pi_translate.py`; no anonymized real transcripts (see Unresolved limits) |
| Live launch, resume, messaging, interruption and approval per integration; fallbacks | `tests/live/`; this record |
| Local threat model and secret-redaction tests | `docs/threat-model.md`; `test_redact.py` |
| Indexes tested with large, malformed, truncated and actively written files | `test_tailer.py`, `test_ingest.py`, `test_discovery.py` |
| Keyboard-only dashboard and context-sharing scenario | `tests/e2e/test_web.py`, `web/src/__tests__/components.test.tsx` (WP-4.5) |
| Backup, export and restore instructions | `README.md` (Backup and restore) |
| Partial observation, unknown state, inferred attribution, unsupported controls, experimental paths labelled | Session and finding views; `docs/capability-matrix.md` |
| Event replay, expired approvals, unsupported controls, interrupted runs, daemon restart, browser reconnect without duplicate actions | Also verify and Recovery evidence above |
| Managed vs observed sessions, agent-reported completion vs verified checks | `README.md` (Sessions); dashboard state labels |

## Unresolved limits

- **Pi hook test timed out once.** `test_pi_observer_fires` hung for 180 s in one suite run, then passed 6 of 6 runs alone (about 5 s each). Robin's Pi observer launches its relay detached and never waits for it, so it can't hold Pi open; a stalled provider call is the likely cause, but this wasn't confirmed.
- **`uv tool install` doesn't use `uv.lock`.** It resolved claude-agent-sdk 0.2.168, while the lock has 0.2.167. The release scenario passed on 0.2.168. `robin doctor` shows the installed SDK version.
- **Terminal handoff isn't re-run live for the release.** It is covered with the scripted runtime (`test_handoff.py`) and was verified live for all three runtimes in Phase 0 (`docs/capability-matrix.md`).
- **Codex hooks haven't fired live.** The installer matches the shape of Codex's own hooks file (Phase 2); Codex sessions started outside Robin are still seen through their transcripts.
- **Pi reviewers aren't isolated.** Pi has no sandbox; independent Pi sessions are labelled "not isolated".
- **No anonymized real-transcript fixtures.** Adapter tests use synthetic records shaped like each runtime's transcripts; the PRD §20 item asks for anonymized real ones.
- **Phase 0 copy-paste baseline** (`docs/implementation-plan.md`, WP-0.3) is still not recorded.
- Known limits in `docs/threat-model.md`: an unsandboxed agent can read `cli.token`; process discovery with `pgrep` is a heuristic; backups contain prompts; the CLI token has no recognisable prefix.
