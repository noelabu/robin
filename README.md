# Robin

A local workspace for running, observing and coordinating coding agents: Claude Code, Codex and Pi.

Robin launches agent sessions under a supervisor that keeps them alive when the service restarts, indexes the sessions you already have, and lets agents working on the same task exchange findings. You choose exactly what gets shared, and preview it before it is sent. Everything runs on your machine; the dashboard listens on loopback only.

- Product requirements: `docs/Robin_PRD.docx` (v0.5)
- Technical requirements: `docs/Robin_Technical_Requirements.docx` (v0.4)
- Implementation plan and status: `docs/implementation-plan.md`
- Runtime capabilities: `docs/capability-matrix.md`
- Threat model: `docs/threat-model.md`
- Benchmarks: `docs/benchmarks.md`

## Requirements

- macOS, Python 3.13, [uv](https://docs.astral.sh/uv/)
- Any of the runtimes, each signed in with its own login: `claude` (Claude Code), `codex`, `pi`. Robin never reads their credentials.
- Node.js, only to rebuild the dashboard

## Install

```bash
uv tool install .            # puts `robin` on your PATH, dashboard included
robin doctor                 # runtimes, storage, port, supervisor
```

For development, `uv sync` and prefix commands with `uv run`.

## Use

```bash
robin                        # start the service and open the dashboard (or open it if already running)
robin serve --no-browser     # run the service only
robin open                   # a fresh sign-in link for the dashboard
```

Robin's data lives in `~/Library/Application Support/Robin` (override with `--data` or `ROBIN_DATA_DIR`).

### Sessions

```bash
robin project add ~/code/app
robin task create "Review the PRD" --project ~/code/app --attach docs/prd.md
robin run claude --task <task> --prompt "Review docs/prd.md"
robin sessions
robin message <session> --text "Focus on the auth section"
robin interrupt <session>
robin attention              # questions, approvals and failures waiting on you
robin approve <attention-id> # or: robin deny <attention-id>
robin open <session>         # continue the session in this terminal; Robin hands over control
robin reclaim <session>      # take it back
robin stop <session>
robin runs <session>         # each run's branch, base commit, working-tree state and the files it changed
```

`robin run --worktree` starts the session in a new Git worktree on its own `robin/...` branch, under `~/robin-worktrees` (or `ROBIN_WORKTREES`), so parallel sessions in one repository don't edit the same files. `robin worktree list` shows them; `robin worktree remove <id>` shows what removal would lose, refuses while the session runs, needs `--discard-changes` for uncommitted files, and always keeps the branch and its commits.

Managed sessions are ones Robin launched; it can control them. Observed sessions are ones you started elsewhere; Robin reads their history and never changes them.

```bash
robin scan                   # index your existing Claude, Codex and Pi sessions (read-only)
robin watch on               # follow native history as it grows
robin hooks install claude   # opt-in live events for sessions started outside Robin (shows the change first)
```

### Findings and context

Each agent publishes findings with cited evidence. Agents with Robin's tools publish by tool call; the rest publish in a fenced block that Robin parses from the message.

```bash
robin findings --task <task>
robin compare --task <task>
robin share --task <task> --to <session> --question "Reconcile these" --findings <id>,<id>
robin context preview --task <task> --to <session> --question "..." --findings <id>,<id>
robin context deliver <packet> --confirm <payload-hash>
robin synthesize --task <task> --to <session> --findings <id>,<id>
robin search "token refresh"
```

Nothing reaches another agent until you confirm the exact payload hash. Secrets are redacted before anything is stored, indexed or shared. Reviewers launched with `--independent` cannot read other agents' findings unless you grant them. A session is never handed its own output back, directly or through a finding built on it, unless you include it with `--allow-loop`.

### Backup and restore

```bash
robin backup --to ~/robin-backups               # writes robin-backup-<time>/ there; no credentials
robin restore ~/robin-backups/robin-backup-<time> --into ~/robin-restored
robin export --task <task> --out ~/robin-export   # tasks, findings and syntheses as JSON and Markdown
```

A restore goes into a new, empty data directory and is checked before anything is written. Point Robin at it with `--data` or `ROBIN_DATA_DIR`.

### Operations

```bash
robin doctor                         # health of runtimes, storage, port and supervisor
robin storage                        # copied activity against the 20 GB cap; `robin storage prune`
robin supervisor restart             # upgrade the supervisor; refuses while turns run (or --drain)
robin diagnostics                    # a redacted bundle, previewed before it's written
robin notify on                      # macOS notifications when a session needs input, fails or finishes
```

Logs are in `<data dir>/logs/robin.log` and never contain prompts, messages or secrets.

## Tests

```bash
uv run pytest                                  # Python suite
cd web && npm test                             # web components
uv run playwright install chromium             # once, if Google Chrome isn't installed
ROBIN_E2E=1 uv run pytest tests/e2e            # the dashboard in a real browser
ROBIN_PACKAGING=1 uv run pytest tests/packaging  # build the wheel and run it from a clean venv
ROBIN_LIVE=1 uv run pytest tests/live          # real Claude, Codex and Pi sessions (uses your logins)
```

Rebuild the dashboard after changing `web/`: `cd web && npm run build` (output goes to `src/robin/web/dist`).

## Phase 0 spikes

`supervisor/`, `claude/`, `codex/`, `pi/` and `corpus/` hold the Phase 0 integration spikes and their findings. They are the evidence behind `docs/capability-matrix.md` and are not part of the product.
