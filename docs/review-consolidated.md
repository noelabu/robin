# Consolidated review: Robin PRD v0.2 and Technical Requirements v0.1

- **Date:** 2026-10-14
- **Inputs:** `review-gpt-sol6.md` (GPT), `review-kimik3.md` (Kimi), `review-opus5.5.md` (Opus)
- **Documents reviewed:** `Robin_PRD.docx` v0.2, `Robin_Technical_Requirements.docx` v0.1
- **Outcome:** every finding was checked against the source text. The confirmed findings are applied in PRD v0.3 and Technical Requirements v0.2.

## Verdict

The three reviews agree. The documents are strong on honest state, action IDs, `outcome_unknown`, no blind retries, and "an ended turn is not a finished task". They are ready for Phase 0 validation but not yet for broad implementation. Four architectural gaps had to be closed first: the independence boundary, session reuse across tasks, terminal ownership, and how findings get published. Some decisions also still depended on how the runtimes behave: session survival across restarts and approval ownership.

Verification key: **Confirmed** means the source text supports the finding. **Partial** means the finding is valid but overstated, or partly covered already. **Not applied** means it was rejected, or left as an owner decision.

## A. Architectural findings (High)

| # | Finding | Raised by | Verification | Resolution (PRD v0.3 / TRD v0.2) |
|---|---|---|---|---|
| A1 | Independent reviewers can read each other's findings through their own filesystem tools. PRD §9 puts findings in `.robin/tasks/.../findings/` inside the repo. TR-066 only scopes MCP. | GPT R-01, Opus #2 | **Confirmed.** FR-032 and TR-064 promise independence, but nothing enforces it at the filesystem level. Even the app-data store (TR-021) can be read by agents that have unrestricted read access. | FR-032 now separates *not injected* from *enforced isolation*. Unshared findings are never written inside a project or execution directory. Each adapter reports whether it enforces independence, and the UI labels reviews that aren't isolated. New TR-064 text. §9 task directory replaced by the artifact store. Acceptance test added. |
| A2 | Reusing a native session for a different task brings that task's earlier context with it. Late events can also be attributed to the wrong task. | GPT R-02, Opus #2 | **Confirmed.** TR-020 allows many-to-many links. "Task switching within a resumed native session" is still an open decision. | Work on a different task starts a new native session by default. Continuing an existing session needs confirmation and discloses the context it keeps. A run's task is fixed when the run starts (FR-013, TR-020). |
| A3 | The native-terminal fallback has no ownership contract. Robin and the terminal can both drive one session, and FR-013's "do not duplicate an active run" can't be enforced. | GPT R-03, Opus #5 | **Confirmed.** TR-042 serializes only the actions that go through Robin. | New TR-046 control lease. Each adapter declares a terminal mode: `attach`, `handoff` or `unavailable`. During a handoff, Robin's controls are disabled and its pending attention items move to `handed_off`. Control returns only after reconciliation (FR-037). |
| A4 | Publishing findings is P0, but the only path is MCP, which is optional. A file drop can't be attributed, and sandboxed agents can't write to app-data. The PRD allows findings written by the user, but the TR-060 author must be a runtime. | GPT R-04, Opus #1 | **Confirmed.** PRD §18 still lists "files and adapter prompts" as open. | New TR-067. Every adapter must support at least one validated path: task-scoped MCP `finding.publish`, or a structured final-message block parsed from the session's own event stream. Attribution comes from the emitting session, never from file contents. Author is either a user or an agent. Findings get submission IDs, immutable versions, and visible rejections. FR-020 updated. §18 decision resolved. |
| A5 | Whether sessions survive a Robin restart is undecided, yet acceptance step 10 depends on it. | Opus #3 | **Partial.** TR-044 and TR-072 already say to "report that limit and offer native resume", but the acceptance scenario assumes sessions survive. | Made a Phase 0 go/no-go item. Acceptance step 10 and the TRD release scenario now cover both outcomes. §18 updated. |
| A6 | When Robin launches a session, it effectively becomes the approval layer. For example, the Claude Agent SDK caller sets the permission mode, settings sources and approval callback. "Expired request" is undefined for runtimes that have no native expiry. | Opus #4 | **Confirmed.** FR-012 and TR-010 don't specify a permission mode or which settings load. | FR-012 and TR-010 now record permission mode, sandbox and settings sources. A managed session loads the user's harness settings by default, and Robin never picks a mode more permissive than the harness default. TR-045 defines expiry: native expiry where the runtime has it; otherwise the request expires when the run ends, is interrupted, or the connection generation changes. |

## B. Contract findings (Medium)

| # | Finding | Raised by | Verification | Resolution |
|---|---|---|---|---|
| B1 | Task storage and the execution working directory are conflated. FR-012 says "launch in the task directory", but TR-021 puts the store in app-data. | GPT R-05 | **Confirmed** (wording conflict). | `project_root`, `execution_cwd` and `artifact_store` are now separate (FR-012, TR-021). By default the execution directory is the project root or the selected worktree. |
| B2 | Agent retrieval grants have no record, lifecycle, revocation or version binding. MCP reads produce no manifest. | GPT R-06 | **Confirmed.** | New TR-068: grant records bound to the session, task, exact versions or ranges, and the approving action. Grants can be revoked, and every MCP read is logged as a delivery against a manifest. Grant record added to the TR-020 table. |
| B3 | Quarantining a malformed record can block the checkpoint from advancing, so ingestion keeps hitting the same bad record. | GPT R-07 | **Confirmed** (ambiguity). | TR-033: a complete malformed record commits its quarantine marker and the checkpoint advance together. An incomplete tail stays pending. |
| B4 | Installing hooks for observed sessions changes the harness config, which conflicts with read-only discovery. | Opus #7 | **Confirmed.** | FR-041 and PRD §12: installing hooks is opt-in, previewed, reversible and never automatic. TR-070 row added. |
| B5 | Shared findings and excerpts can carry prompt injection from tool or web output. Today they're treated as untrusted only when rendered in the browser. | Opus #8 | **Confirmed.** | New TR-069: packets frame shared content as quoted data with source labels, and the preview flags excerpts that contain tool or web output. PRD §12 and the §17 risk table updated. |
| B6 | Scope is large for one developer, and TR-091 ties the release to the weakest adapter. | Opus #6 | **Confirmed as a risk.** Cutting scope is the product owner's call. | **Partly applied.** Phase 0 now defines a minimum capability floor per runtime. TR-091 now says that reducing the runtime scope needs an explicit, recorded product decision. Moving FR-025 or export to P1 is listed in §18 as an open decision; the priorities were not changed. |

## C. Cross-document inconsistencies

| # | Finding | Raised by | Verification | Resolution |
|---|---|---|---|---|
| C1 | Storage location: the PRD uses `.robin/` in the repo, the TRD uses app-data. | Opus | **Confirmed.** | The TRD now owns this decision: app-data by default, `.robin/` only through an explicit export. PRD §9 and §18 point to it. |
| C2 | The finding schemas differ: YAML vs JSON, path-string evidence vs `artifact_id`+range, no `created_at` in the PRD, and no `id` or version in the TRD. | Kimi #2, Opus | **Confirmed.** | One structured schema, owned by TR-060, with `finding_id`, `version`, an author union, `created_at`, structured evidence and `parents`. The PRD example now mirrors it. |
| C3 | The MCP tool lists differ: the PRD lists `session.search` and TR-066 doesn't. | Kimi #1 | **Confirmed.** | `session.search` added to TR-066, limited to sessions granted to the task. |
| C4 | The PRD lists delivery and control-action states without `outcome_unknown` or `cancelled`. | Kimi #4 | **Confirmed.** Also true of the PRD §9 Control action row. | PRD §9 and §11 now include them. |
| C5 | The integration references differ: the PRD Appendix C cites the Codex blog, Claude headless docs and the Pi SDK; the TRD cites the app-server, Agent SDK and RPC docs. | Kimi #3 | **Confirmed.** | PRD Appendix C now uses the canonical references, with Pi SDK kept as secondary. |
| C6 | An AttentionItem requires a native request ID, but items for "findings awaiting review" don't have one. | Opus | **Confirmed.** | The native request ID is now optional, and the item kinds are listed. |
| C7 | Traceability maps FR-050–051 to TR-070–072, but no TR covers refreshing Git metadata. | Opus | **Confirmed.** | New TR-075. Traceability row updated. |
| C8 | PRD §10 restates the architecture, which caused drift. | Opus | **Confirmed.** | **Partly applied.** §10 now says the TRD is authoritative, rather than being cut. |

## D. Smaller items

| # | Finding | Raised by | Verification | Resolution |
|---|---|---|---|---|
| D1 | The CLI doesn't match the examples: `synthesize --from`, the `share` syntax, `--from codex` being ambiguous, and no non-interactive way to confirm a share. FR-024 selects findings, not agents. | Kimi #5, Opus | **Confirmed.** | Appendix A and the §7 examples now use `--findings` IDs, session references, and `robin context preview` / `deliver --confirm HASH`. |
| D2 | The TRD has no CLI→API contract. | Kimi | **Confirmed.** | New TR-053 with a mapping table. |
| D3 | FR-001's acceptance criteria require remote and branch even for an ordinary directory. | Kimi | **Confirmed.** | "when applicable". |
| D4 | The 90% target for session recovery has no denominator. | Kimi | **Confirmed.** | Denominator defined: the Phase 0 corpus per validated runtime version. |
| D5 | The PRD doesn't say who completes a task, and the task status values are never listed. | Kimi, Opus | **Confirmed.** | FR-014: the user completes a task. Statuses are `open`, `completed` and `abandoned`; archive is a separate visibility flag. |
| D6 | NFR-01 mixes startup latency with corpus size. | Kimi | **Partial.** It reads as a condition, and TR-080 already clarifies it. | Wording tightened. |
| D7 | Targets that can't be measured: "materially smaller", "primary interaction", and NFR-02's corpus. | Opus | **Partial.** FR-030 (three interactions to reach a session) and §14 (two to reach a pending request) measure different things, so they don't contradict each other, but "interaction" is undefined. | "Primary interaction" defined. A proposed packet-size ratio added. NFR-02 bound to the NFR-01 dataset. |
| D8 | The success measures are all acceptance tests, with no outcome measure. | Opus | **Confirmed.** | Added "Time to cross-agent review" against a copy-paste baseline. |
| D9 | TR-071 doesn't check for tmux. | Opus | **Confirmed.** | The doctor reports tmux as optional, and terminal behaviour without it is defined. |
| D10 | Subscription logins could change the cost model. | Opus | **Confirmed.** | Phase 0 exit check per runtime. |
| D11 | The search index can hold plaintext secrets. | Opus | **Confirmed.** | Secrets are scanned and masked at index time (TR-070, PRD §12). |
| D12 | TR-051 bootstrap secret: no entropy, expiry or rate-limit rules. | Kimi | **Confirmed.** | Added ≥128 bits, 60 s expiry, invalidation on failure, and rate limiting. |
| D13 | The relationship between payload limits is unclear (256 KiB packet, 1 MiB parser, 64 KiB display). | Kimi | **Confirmed.** | TR-062 clarifies it. |
| D14 | TR-073/074 map only to the launch checklist. | Kimi | **Partial.** The traceability row "NFR-01–11 → TR-070–084" does cover them, but no FR does. | Kimi suggests promoting this to an FR, while Opus suggests demoting export to P1. Left in §18 as an owner decision. |
| D15 | Conflicting controls from two browser tabs. | Kimi | **Partial.** TR-042 already rejects stale preconditions; only the UX was missing. | TR-042: the first committed action wins, and the second gets 409 with the current state. |
| D16 | The user-facing experience of adapter failure. | Kimi | **Partial.** NFR-06 already requires "a clear warning". | NFR-06 now names a degraded state on the dashboard plus `robin doctor` guidance. |
| D17 | Notification scope. | Kimi | **Partial.** The doc is already clear: FR-033 is P1 and FR-035 is P0. | One clarifying sentence added to FR-033. |
| D18 | Formatting: headings lost their punctuation, items that should be bullets are plain paragraphs, the FR-014/FR-023 "P0" isn't bold, and the TRD uses page-number references. | Kimi, Opus | **Confirmed** in the docx XML. These are real defects in the file, not just pandoc conversion artifacts. | All fixed. The TRD map now uses requirement ranges instead of page numbers. |

## E. Not changed

The reviewers asked to keep these as they are: the phased plan, the "fixtures alone don't prove compatibility" gate, the rule against auto-approval, and the explicit `outcome_unknown` handling.

## Owner decisions (resolved 2026-10-15, PRD v0.4)

1. **Runtime scope:** a runtime that misses the minimum capability floor (PRD §16) ships observed-only, and the other runtimes ship with live control. The release record names each runtime's level.
2. **Priorities:** FR-025 (loop prevention) moves to P1. The P0 context preview still labels derived artifacts. Backup and export become their own P0 requirement, FR-043.
3. **Restart survival:** a detached session supervisor owns managed sessions, so they survive a restart of the Robin service. If the supervisor itself crashes, sessions show as disconnected and the user resumes them natively; nothing relaunches automatically.

Technical Requirements v0.3 apply these decisions:
- TR-006 defines the session supervisor.
- TR-002, TR-044, TR-071, TR-072 and TR-084 are updated to work with the supervisor.
- TR-065 records that FR-025 is now P1.
- TR-073 and TR-074 now trace to FR-043 and include a restore test.
- TR-091 adds the observed-only release rule.
- The release boundary and the traceability table are updated to match.
