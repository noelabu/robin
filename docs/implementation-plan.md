# Robin implementation plan

- **Date:** 2026-10-15
- **Baseline:** Robin PRD v0.5, Technical Requirements (TRD) v0.4, `review-consolidated.md`
- **Assumes:** one developer, one execution host, macOS only for the first release (see Owner decisions)
- **Authority:** where this plan and the TRD differ, the TRD wins and this plan is updated

The plan follows the PRD §16 phases (0–4). Each phase ends on demonstrated behaviour, not screen count (TRD delivery sequence). Work packages (WP) name the requirements they close, so the traceability table in the TRD can be checked against finished work.

## Owner decisions (2026-10-15)

| Decision | Effect on the plan |
|---|---|
| Claude Agent SDK is the first adapter; an API key is acceptable if the SDK won't take a subscription login | Claude is built against an API key if needed. Codex app-server becomes the first adapter only if Claude is unavailable or misses the floor. |
| Claude, Codex and Pi all have accounts | All three are spiked in Phase 0 and classified live or observed-only against the floor. |
| macOS only for the first release | Linux validation is dropped from Phase 0 and the release gate. **This differs from PRD v0.4 ("macOS and Linux") and needs a PRD/TRD revision.** |
| Reference and development machine is a Mac mini M4 | It replaces the TRD's proposed 8-core/16 GB reference (TR-080). Exact core count and RAM to be recorded in WP-0.3. |
| Private project | No public license, no PyPI publishing. Fixtures stay anonymized, as the PRD launch checklist requires, but are not published. The name check only needs to avoid clashes on your own machine and in your package index. |
| Worktrees not needed | FR-051 stays P1 (see P1 table). |
| No remote access in v1 | Loopback only. Remote use goes through SSH port forwarding (`ssh -L`). Private-network access (UC-05, TR-052) moves after the MVP. |
| Opt-in hook installers for every runtime that has hooks | New WP-2.6. Sessions started outside Robin get live events where a runtime supports hooks; others fall back to transcript tailing. Phase 2 grows by about 2 weeks. |
| Copied activity capped at 20 GB | Above the cap, the oldest copied activity and transcript text is pruned, with a warning at 80%. Findings, manifests and audit records are never pruned. Native transcripts are untouched. Other TRD limits unchanged. |
| Secret scanner uses gitleaks' rule set plus custom patterns | The rules are vendored as data and run in Python; there is no gitleaks binary dependency (WP-3.3). |
| Keep the name "Robin" | Command and package `robin`. Re-check `which robin` on the Mac mini in Phase 0. |
| Trunk-based: commit directly to `main` | No phase or work-package branches and no PRs. Phase 0 code lives at the repo root (`supervisor/`, `claude/`), not in a separate `spikes/` folder. Run `/review-lite` before committing a work package, since there is no PR review step. |
| Revise PRD and TRD now | Done: PRD v0.5 and TRD v0.4 record these decisions. Hook installers are TR-076. |

Durations are rough single-developer estimates. They are for sequencing, not commitments.

| Phase | Goal | Rough size |
|---|---|---|
| 0 | Prove each runtime's integration against the capability floor | 2 weeks |
| 1 | One managed session end to end, surviving a service restart | 5 weeks |
| 2 | Projects, tasks, all adapters at their Phase 0 level, attention, terminal handoff, hook installers | 6 weeks |
| 3 | Findings, context packets, manifests, grants, synthesis | 4 weeks |
| 4 | Search, history at scale, backup/export, packaging, perf, a11y, release | 4 weeks |
| P1 | Loop exclusion, notifications, Git context, worktrees | after P0 slice |

---

## Adjustments to the PRD phase table

Three items move earlier than PRD §16 lists them. Each one is a dependency, not a scope change.

1. **Browser auth and origin checks move from Phase 4 to Phase 1.** Phase 1 exposes live session controls over HTTP. Shipping that without TR-051 (bootstrap secret, HttpOnly cookie, Origin/Host checks, CSRF) would leave an unauthenticated local control endpoint. Phase 4 keeps the security *audit*, not the first implementation.
2. **Discovery and history parsing (FR-010/011) move from Phase 4 to Phase 2.** Attaching an existing session to a task (FR-003) and shipping an observed-only runtime both need discovery. Phase 4 keeps FTS search, the 10,000-session dataset and the performance work.
3. **Migrations with a pre-migration backup (TR-073) start in Phase 1.** The schema changes every phase after that. The user-facing backup, export and restore (FR-043/TR-074) stay in Phase 4.

---

## Proposed repository layout

```
robin/
  pyproject.toml                 # uv; Python 3.12+, locked versions (TR-003)
  src/robin/
    cli/                         # thin client of /api/v1, no own endpoints (TR-053)
    service/                     # FastAPI app, auth, routes, SSE (TR-050–052, 034–035)
    supervisor/                  # detached process, socket protocol, event spool (TR-006)
    runtime/                     # runtime manager: action ledger, control lease, reconcile (TR-040–046)
    adapters/
      base.py                    # AgentAdapter protocol + capability model (TR-010–011)
      fake/                      # scripted adapter for deterministic tests
      codex/  claude/  pi/
    events/                      # envelope, ingestion, dedup, checkpoints (TR-030–033)
    store/                       # SQLAlchemy models, Alembic, artifact store (TR-020–023)
    findings/                    # schema, publication, packets, manifests, grants (TR-060–069)
    search/                      # FTS5 index, masking (FR-040)
    security/                    # path validation, secret scanner, process allowlist (TR-070)
    git/                         # metadata reader, worktrees (TR-075, FR-051)
    mcp/                         # task-scoped tools (TR-066)
  web/                           # React + TypeScript (Vite); built assets bundled in the wheel
  tests/
    fixtures/{codex,claude,pi}/  # anonymized transcripts from the Phase 0 corpus
  supervisor/                    # Phase 0 supervisor and runtime-host spike (evidence)
  claude/                        # Phase 0 Claude Agent SDK findings
  codex/                         # Phase 0 Codex app-server findings
  docs/capability-matrix.md      # dated per OS × runtime version (TR-090)
```

The **fake adapter** is a first-class deliverable. It implements the full adapter contract with scripted event streams, so crash-at-boundary, replay, approval-expiry, outcome_unknown and lost-acknowledgement tests run without a real runtime. Real-runtime tests still gate the release (TR-090: mocks don't prove native compatibility).

---

## Phase 0 — Integration validation (2 weeks)

**Goal:** a dated capability matrix with live evidence, and each runtime classified as **live** or **observed-only** against the PRD §16 floor.

### WP-0.1 Runtime spikes (one per runtime: Codex app-server, Claude Agent SDK, Pi RPC)

For each runtime, on macOS, record the exact version, account method, launch arguments, session storage location and permission settings. Then prove or classify as unsupported:

| Floor item (PRD §16) | Evidence to capture |
|---|---|
| Launch with native ID capture | Native session ID returned before or with the first event |
| Structured event streaming | Event families covered (TRD TR-030 table) |
| Follow-up message | Which input modes exist: new turn, queued follow-up, steer |
| Interruption | Acknowledgement signal and resulting state |
| Approval response or native fallback | Request ID, expiry semantics, what happens with no callback |
| One finding publication path | MCP `finding.publish` **or** a parsed final-message block (TR-067) |
| Hosting under the supervisor | Process survives the parent (stand-in for the service) being killed and reattached |

Also record: the hook mechanism and its config location (for WP-2.6), native resume, terminal mode (`attach` / `handoff` / `unavailable`), whether the sandbox can exclude the artifact store (review independence, TR-064), subscription vs API-key acceptance and its cost implication (TR-012), and whether a Node sidecar is needed (Pi SDK).

### WP-0.2 Supervisor transport prototype

A minimal detached process holding one runtime child over a Unix socket. Kill and restart the "service" side; confirm the child keeps running and buffered events replay from a cursor. This answers whether any transport needs tmux (TR-006, TR-071).

### WP-0.3 Corpus and baselines

- Collect a real session corpus per runtime version. Anonymize a subset into `tests/fixtures/`. The corpus is the denominator for the 90% session-recovery target (PRD §14).
- Time the copy-and-paste cross-agent review on a real PRD. This is the baseline for the "time to cross-agent review" measure.
- Record the reference machine: the Mac mini M4, with chip variant, core count, RAM, storage and macOS version (TR-080).

### Phase 0 exit

- `docs/capability-matrix.md` with each runtime marked live or observed-only, and the reason for each gap.
- Supervisor transport plan, including which runtimes need tmux.
- Publication path chosen per runtime.
- **Phase 1 adapter confirmed.** Owner decision 2026-10-15: **Claude Agent SDK first**, because Claude Code is the owner's main runtime and Phase 1 can be used on real work straight away. Fall back to Codex app-server if Claude misses the floor. Two Claude checks decide this and must be done first in WP-0.1:
  - **Account:** whether the SDK accepts the owner's Claude subscription login for this use, or needs an API key, and what that costs (TR-012).
  - **Hosting:** whether the SDK runs inside a per-session runtime-host process under the supervisor and keeps working after the service is killed (WP-1.3).
- Resolve the open TRD decisions that Phase 0 can answer: terminal mode per adapter, sidecar packaging, supported versions.

---

## Phase 1 — One managed session (5 weeks)

**Goal:** launch, observe, send a follow-up, interrupt, reconnect the browser, and survive a service restart — for one runtime.

### WP-1.1 Project skeleton and storage foundation (TR-003, 020–023, 073)

- uv project, lint/type/test CI, Alembic with a schema-compatibility check and an automatic pre-migration backup via SQLite's backup API.
- SQLite in WAL mode, foreign keys, one bounded writer queue, short transactions (TR-004).
- Data directory in OS app-data, mode 0700/0600 (TR-021, TR-070). Keep `project_root`, `execution_cwd` and `artifact_store` as separate types so they can't be mixed up.
- Atomic artifact writes: temp sibling, fsync, rename, then commit the metadata reference. Reconcile orphans on startup (TR-022).
- Initial tables: Session, Run, Connection, Event, SourceCheckpoint, ControlAction, AuditEntry. The unique constraints from TR-023 go in from the start.

### WP-1.2 Event envelope and ingestion (TR-030–033)

- Pydantic envelope with every TR-030 field, including `evidence_kind` (reported / observed / inferred / unknown).
- Global cursor = commit order. Per-source order from native sequence or parser offset.
- Dedup by native event identity, otherwise source generation + byte range. Content hash alone never collapses events.
- Commit the event and its checkpoint in one transaction before any browser notification.

### WP-1.3 Session supervisor (TR-006, 044, 072)

- Detached process with an OS lock per data directory. Versioned, typed protocol over a user-restricted Unix socket, with correlation IDs.
- Owns runtime children. Spools events to disk until the service acknowledges a cursor, then replays unacknowledged events on reconnect.
- Process identity is checked with more than the PID (e.g. PID + start time + launch token).
- **Runtime hosts.** Runtimes with an in-process SDK (Claude Agent SDK first) run inside a small per-session runtime-host child process. That process speaks the supervisor's framed protocol over stdio, so the supervisor itself stays runtime-agnostic. The SDK's approval callback (`can_use_tool`) lives in the runtime host and blocks while the request travels through the supervisor to the service, where it becomes an attention item. The service answers it with the native request ID and generation (TR-045). If the service is down, the request waits and is never answered automatically. Adapter upgrades affect only newly launched hosts, so the supervisor needs no drain for them (TR-006).
- Supervisor exit: sessions become `disconnected` and native resume is offered. Nothing relaunches.
- `robin supervisor stop` is a confirmed action that names the sessions it ends.

### WP-1.4 Runtime manager, action ledger and state model (TR-040–045)

- Separate runtime state, connection state, attention and task status (TR-040). Transitions follow the TR-040 signal table.
- Persist the action before dispatch. States: requested → submitted → acknowledged / failed / cancelled / outcome_unknown.
- Idempotent `action_id`: an identical repeat returns the stored outcome; an ID reused with a different payload gets 409.
- Per-session mutation serialization. Revalidate capability and connection generation just before dispatch. First committed action wins; the other gets 409 with current state.
- A crash between submit and acknowledgement → `outcome_unknown`, reconciled through native correlation. Never resend automatically.
- Interrupt stays pending until acknowledged. Force-terminate is a separate, named, confirmed action.

### WP-1.5 Adapter contract and first adapter (TR-010–012, FR-012/015/016/018)

- `AgentAdapter` protocol and capability model with per-operation support, unsupported reasons, terminal mode, publication path and independence flag.
- Fake adapter (see layout).
- First real adapter, Claude Agent SDK: probe, launch, events, send (streaming-input mode for follow-ups), interrupt, approval callback, reconcile. Set `setting_sources` explicitly so the user's Claude Code settings load; don't rely on the SDK default. Record permission mode, sandbox and settings sources at launch. Load the user's harness settings by default and never choose a mode more permissive than the harness default.
- Process launch through an executable allowlist and argument arrays only (TR-070).

### WP-1.6 HTTP API, auth and SSE (TR-050–052, 034–035)

- `/api/v1`: `GET /workspace`, `GET /adapters`, `GET /sessions[/{id}]`, `POST /sessions/launch`, `POST /sessions/{id}/actions`, `GET /actions/{id}`, `GET /events`, `GET /stream`.
- Loopback binding. Bootstrap secret in the URL fragment (≥128 bits, 60 s expiry, single use, rate-limited, never logged) exchanged for an HttpOnly cookie. Origin/Host validation, CSRF on mutations, no wildcard CORS.
- Error shape: code, safe message, correlation ID, retry guidance. 202 + action record for async actions.
- Snapshot and watermark read in one transaction. SSE IDs are durable cursors. `Last-Event-ID` resume, `resync_required` on an invalidated cursor, heartbeats, and slow clients are reconnected rather than silently dropped.
- CLI auth over a user-restricted local credential (TR-052).

### WP-1.7 Minimal web client (FR-030 subset, FR-034)

- Vite + React + TS. Session card, live activity feed, launch / message / interrupt controls driven by the capability report. Disabled controls show their reason.
- Sanitized Markdown rendering from day one: no raw HTML, scripts, remote resources or unsafe links (TR-070).
- Client dedups by event ID and upserts projections. Shows freshness and gaps.
- Keyboard operable with visible focus from the first component (NFR-08). Retrofitting accessibility later costs more.

### WP-1.8 CLI (TR-053 subset)

`robin`, `robin serve`, `robin run`, `robin message`, `robin interrupt`, `robin doctor`. All are API clients.

### Phase 1 exit (demonstrated)

- Launch a real session, see structured activity in the browser, send a follow-up, interrupt with acknowledgement.
- Refresh and close the browser: no relaunch, no duplicate history.
- Kill the service mid-turn and restart it: the session keeps running under the supervisor, history has no duplicates, no input is resent.
- Kill the service between dispatch and acknowledgement: the action shows `outcome_unknown` and is not resent.
- Kill the supervisor: the session shows `disconnected` and native resume is offered.
- Origin/CSRF rejection tests pass.

---

## Phase 2 — Shared workspace (6 weeks)

**Goal:** several real sessions, from every runtime, visible under tasks and controllable only where supported.

### WP-2.1 Projects and tasks (FR-001–003, TR-020, TR-075 subset)

- Register a Git repo or plain directory. Path canonicalization and symlink checks (TR-070). Read remote and branch off the request path; a non-Git directory is valid with no Git fields.
- Tasks with status `open` / `completed` / `abandoned` and a separate `archived` flag (FR-014). Only an explicit user decision with recorded evidence completes a task.
- Attachments as approved snapshots with hashes; originals untouched (FR-002).
- TaskSession associations. A run's task is fixed when the run starts, and late events keep that attribution. Work for another task starts a new native session by default; continuing an existing one needs confirmation and discloses the retained context (FR-013, TR-020).
- `PATCH` with expected revision.

### WP-2.2 Remaining adapters at their Phase 0 level (FR-010–018, Appendix B)

- Live runtimes: the full Phase 1 contract plus resume and approval response.
- Observed-only runtimes: discover, parse and native terminal launch, with live controls disabled and the reason shown.
- Every adapter: discovery that never modifies sources; parsers that tolerate unknown fields, incomplete tails, rotation and truncation (TR-033); quarantine of complete malformed records with the checkpoint advance in one transaction.
- One failing adapter must not stop the others (NFR-06). Show a degraded state on the dashboard with `robin doctor` guidance.
- Fixture tests per adapter from the Phase 0 corpus.

### WP-2.3 Attention queue (FR-017, FR-035, TR-045)

- AttentionItem kinds: question, approval, failure, finding_review. Native request ID is optional.
- Approval responses atomically claim the request, dispatch once and record the outcome. Expiry follows TR-045. Expired or resolved requests reject replies. Dismissing never approves.
- Acknowledge / resolve / dismiss each write an audit record; a new event can reopen an item.

### WP-2.4 Native terminal and control lease (FR-037, TR-046)

- One owner per session through a control lease with a generation.
- `attach`: join the same process (tmux where Phase 0 requires it). `handoff`: release the lease, disable Robin's controls, mark pending attention items `handed_off`. `unavailable`: block terminal access while a managed run is active.
- Control returns only after the user returns it and reconciliation succeeds.
- Without tmux, the fallback is the validated native command in the user's own terminal (TR-071).

### WP-2.5 Dashboard and task workspace (FR-030, FR-031, FR-036)

- Project/task navigator, session list, activity feed with project/task/runtime/event filters, a persistent attention panel, a session detail panel.
- Reach an active session or task in ≤3 primary interactions; reach a pending request in ≤2 (PRD §14).
- Show branch and working directory for every write run, and warn when write sessions share a directory (TR-070). With worktrees deferred to P1, this warning is the P0 protection against concurrent edits.
- Evidence labels: reported, observed, inferred and unknown shown separately. Empty states say whether there is no activity or missing coverage. No progress percentages.
- Archive and restore (FR-042).

### WP-2.6 Opt-in hook installers (FR-041, TR-070)

- One installer per runtime whose hook mechanism Phase 0 confirmed. A runtime without hooks stays on transcript tailing, and its capability report says so.
- **Install flow:** detect the harness config file, show a diff of exactly what will be added, write only on confirmation, back up the original, and record the change so it can be undone. Robin's entries sit alongside your existing hooks and never replace or reorder them.
- **Uninstall** removes only Robin's entries, and `robin doctor` flags any drift between the recorded install and the current config.
- **The relay never gets in the harness's way.** Hooks call a small `robin hook-relay` command. It authenticates with the local CLI credential (TR-052), posts the event, and always exits 0 within a short timeout. If Robin is down, the event is dropped and the session shows a coverage gap. The agent is never blocked or slowed.
- **Robin's hooks only observe.** They never return an approval or permission decision, even for tool-approval hooks (TR-070: no auto-approval).
- Hook events go through the normal envelope and dedup (TR-030–032), so a session seen both by hooks and by tailing isn't duplicated.

### Phase 2 exit

- One managed session per live runtime under one task, plus one discovered historical session per runtime attached to it.
- Answer a real approval request, and confirm an expired one is rejected.
- Terminal handoff during an active turn and during a pending approval: never two owners, never a duplicate response.
- Attach a session to a second task: a new native session is the default and late events keep the original task.
- Install hooks for Claude Code, start a session in a normal terminal and see live events; uninstall and confirm the config is back to the original byte for byte.

---

## Phase 3 — Context collaboration (4 weeks)

**Goal:** independent reviews, selected cross-agent critique and synthesis, end to end.

### WP-3.1 Findings (FR-020, TR-060, TR-067)

- JSON schema exactly as TR-060, with immutable versions, submission IDs and an author union (user or agent).
- Publication paths per Phase 0: MCP `finding.publish` and/or a final-message block parsed from the session's own events. Attribution comes from the per-session launch identity, never from file contents or self-reported fields.
- A duplicate submission ID returns the stored result. An invalid submission is recorded as rejected and raises a `finding_review` attention item.
- Unshared findings are stored only in the artifact store, never in a project or execution directory.

### WP-3.2 Review independence (FR-032, TR-064)

- Where the runtime sandbox can exclude the artifact store, launch reviewers with that exclusion.
- Otherwise the capability report says independence is not enforced and the review is labelled "not isolated".

### WP-3.3 Secret scanner and redaction (PRD §12, TR-070)

- gitleaks' rule set vendored as data and run in Python, plus custom patterns from a config file. Record the vendored rules' version, and refresh it deliberately.
- Used for packets now and for index-time masking in Phase 4.
- Matches are never logged (TR-083).

### WP-3.4 Context packets, preview and delivery (FR-021–024, FR-032, TR-061–065, TR-069)

- Deterministic packet build: objective → target question → selected sources → selected findings → bounded excerpts. Findings first; full transcripts only by explicit selection.
- Limits: 256 KiB inline text, 100 messages per excerpt. Never truncate silently; require a smaller selection or an approved summary.
- Preview shows recipient, source versions, excerpt bounds, redactions, bytes and an approximate token count. It flags tool and web output and labels derived artifacts and duplicate hashes (TR-065 P0 part).
- Quoted-data framing with source labels and an explicit instruction boundary (TR-069).
- Confirm locks the payload hash, recipient, source hashes and redactions. A changed source or target invalidates the preview. The approved payload is stored in the artifact store.
- `POST /context/{id}/deliver` persists the manifest and the delivery action atomically. Delivery states follow TR-064; acknowledgement means accepted input, not consumption.

### WP-3.5 Retrieval grants and MCP tools (FR-022/023, TR-066, TR-068)

- RetrievalGrant bound to session, task, exact versions or ranges, and the approving action. Never widens; revocable.
- MCP tools (`task.get`, `finding.list/read/publish`, `artifact.publish`, `session.search`, `session.read_excerpt`) are task- and session-scoped. Every read records a manifest and delivery with the exact content hash.
- MCP stays optional: at least one acceptance run uses the final-message path with MCP disabled.

### WP-3.6 Compare, share and synthesize (FR-024, PRD §7)

- Compare view: findings side by side, select claims, open sources.
- Synthesis = preview + deliver to a chosen session. The result is saved as a new artifact linked to its manifest and input versions (lineage edges).
- CLI: `share`, `context preview`, `context deliver --confirm HASH`, `compare`, `synthesize --findings`.

### Phase 3 exit

- PRD §15 steps 3–8 on live runtimes: three independent reviews, attributed findings (one with MCP disabled), Codex + Pi findings previewed for Claude, confirmed delivery, saved synthesis.
- Independence holds before and after an approved transfer wherever it is claimed.
- A rejected submission, a changed-source invalidation and an over-limit packet all behave as specified.

---

## Phase 4 — History and hardening (4 weeks)

**Goal:** the full PRD §15 acceptance scenario passes on supported environments.

### WP-4.1 Search and incremental indexing (FR-040, FR-041, TR-081–082)

- FTS5 over task names, prompts, responses, findings, file references and metadata, with secrets masked at index time.
- Filesystem notifications plus a bounded polling fallback.
- Indexing exclusions for files, directories, message types and tool outputs.
- 1 MiB parser limit and 64 KiB display limit; larger payloads go to artifact storage with a truncated preview.
- 20 GB default cap on copied activity and transcript text, with a warning at 80% and oldest-first pruning above it. Findings, manifests and audit records never count toward the cap. Pruned excerpts are marked unavailable, keeping their source identity (TR-081). The settings page shows storage usage and a cleanup preview.

### WP-4.2 Backup, export, restore (FR-043, TR-073–074)

- Backup via the SQLite backup API, consistent with artifact files.
- Export with relative paths and hashes, no credentials, and native sources marked as references. An optional `.robin/` export warns about repository visibility.
- Restore into a clean data directory reproduces findings, manifests and hashes.

### WP-4.3 Packaging and operations (TR-071–072, TR-083–084)

- uv/pipx installable wheel with the prebuilt frontend.
- `robin doctor`: FTS5, adapter prerequisites, writable data directory, free loopback port, optional tmux, supervisor health and protocol version.
- Supervisor upgrade requires no active runs or an explicit drain.
- Structured logs without prompts, credentials or scanner matches. Health checks per TR-084. Previewed diagnostic export.

### WP-4.4 Performance (NFR-01–04, 09, 11; TR-080)

- Build the reproducible 10,000-session dataset. Measure startup ≤2 s, search p95 ≤300 ms, idle CPU <1%, 95% of events in the UI within 2 s, and 20 concurrent active sessions.
- Virtualize history lists and bound render batches.

### WP-4.5 Security and accessibility audit (TR-070, NFR-08)

- Local threat model. Hostile Markdown, path traversal and symlink escape, process-argument injection, MCP scope escape, credential-free diagnostics.
- Keyboard-only run of the full acceptance scenario, contrast, non-colour state, announced state changes, reduced motion.

### WP-4.6 Release (TR-090–091, PRD §20)

- Run the full PRD §15 scenario and every "also verify" item on the Mac mini M4.
- Release record: each runtime's live or observed-only level, builds, tested versions, test results, unresolved limits, recovery evidence and benchmark data.

---

## P1 (after the first complete P0 slice)

TR-091: P1 must not delay the first complete P0 vertical slice.

| Item | Requirement | Builds on |
|---|---|---|
| Lineage-based loop exclusion by default | FR-025, TR-065 | Lineage edges from WP-3.6 |
| Notifications | FR-033 | Attention and run events |
| Richer Git context (base commit, working-tree state, changed files per run) | FR-050, TR-075 | WP-2.1 Git reader |
| Optional task worktrees | FR-051, TR-070, TR-072 | Owner decision 2026-10-15: not needed for the first accepted workflow; stays P1 |

---

## Testing strategy (TR-090)

Each test layer starts in the phase that introduces the behaviour and keeps running after that.

| Layer | Starts | Main harness |
|---|---|---|
| Parser and schema | 1 (one adapter), 2 (all) | Phase 0 fixtures; malformed, truncated, rotated, oversized, Unicode |
| Database and actions | 1 | Fake adapter; kill at each transaction and dispatch boundary |
| Adapter contract | 1 | Real installed runtimes, run on demand and before release; not in every CI run |
| Browser integration | 1 | Playwright: snapshot/SSE race, cursor replay, resync, slow client, restart |
| Context collaboration | 3 | Fake + real runtimes; independence, grants, invalidation, limits |
| Security | 1 (auth), 4 (audit) | Origin/CSRF, sanitizer corpus, traversal |
| Performance and accessibility | 1 (a11y), 4 (perf) | Reference dataset; axe + keyboard scripts |

---

## Critical path and risks to the plan

- **Phase 0 is the main schedule risk.** If the supervisor can't host a runtime, that runtime ships observed-only (PRD §16). If it can host none, the restart-continuity decision has to be reopened before Phase 1.
- **Claude API cost.** If the Agent SDK needs an API key, every Robin-launched Claude session is billed per token. That's accepted, but record the cost of the Phase 0 runs so Phase 1 dogfooding has a known budget.
- **The supervisor protocol and action ledger are the hardest code.** They take most of Phase 1. Getting them right with the fake adapter first keeps later adapters cheap.
- **The publication path decides Phase 3.** If no runtime supports reliable MCP, the final-message parser becomes the main path and its format needs a schema version.
- **Adapter drift.** Pin the tested versions. `robin doctor` and the capability report must degrade one adapter, not the app (NFR-06).

## Open items for the owner

None. All decisions to date are in Owner decisions. New questions from Phase 0 results go here.
