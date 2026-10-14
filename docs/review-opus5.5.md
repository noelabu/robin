# Review: Robin PRD v0.2 and Technical Requirements v0.1

- **Documents reviewed:** `docs/Robin_PRD.docx` (v0.2), `docs/Robin_Technical_Requirements.docx` (v0.1)
- **Review date:** 2026-10-14
- **Reviewer:** Claude Opus 5.5

**Verdict:** These are strong drafts. Both documents stay honest about what is reported, observed, inferred or unknown. They also handle failure and recovery carefully: action IDs, the `outcome_unknown` state, no blind retries, and a finished turn never counting as a finished task. Most of the problems are in three places. A few P0 workflows have no defined mechanism. The two documents disagree on storage and schemas. And the MVP is very large for one developer.

## Most important issues

### 1. Agents have no defined way to publish findings, though the workflow depends on it

- FR-020, FR-022 and FR-023 are P0 and require that agents publish and retrieve findings. Step 5 of the acceptance scenario needs one attributed finding from each agent.
- The only way an agent can do this is the MCP tools, which are marked optional (PRD §10, TR-066).
- Writing a file won't work either. TR-021 puts the store in the user's app-data directory, which is outside the repo, so a sandboxed agent (for example Codex in workspace-write mode) can't write there.
- Robin also can't trust who wrote a file.
- **Fix:** make `finding.publish` through MCP P0, with a per-session identity passed in at launch. Or define a structured final-message format that Robin parses. Either way, say how the calling session is identified.

### 2. Agents can see each other's findings before you share them

- FR-032 and TR-064 promise that independent reviewers don't see each other's findings until you share them explicitly.
- But PRD §9 shows findings stored under `.robin/tasks/.../findings/` inside the repo. All three agents can read that folder.
- Resuming a native session also brings its earlier context into a new task (TR-020 lists this as unresolved).
- **Fix:** require that findings live where agents can't read them until shared. Add a test for this to the acceptance scenario.

### 3. Session survival across restarts is undecided, and many requirements depend on it

- Acceptance step 10 and TR-044 expect Robin to "reconcile surviving sessions" after a restart.
- Sessions run as child processes over SDK or stdio usually die when the parent service dies. This is listed only as an open decision (PRD §18 "Runtime persistence").
- The answer decides whether you need a separate supervisor (a detached process or tmux), how approvals survive, and what step 10 can promise.
- **Fix:** move this to Phase 0 as a go/no-go decision, and write both outcomes into the acceptance scenario.

### 4. Approval ownership is unclear when Robin launches the session

- The documents say the harness owns permission decisions and Robin preserves them (PRD §12, TR-070).
- With the Claude Agent SDK, whoever launches the session sets the permission mode, the settings sources and the approval callback. So for managed Claude sessions, Robin effectively *is* the approval layer.
- The launch contract (FR-012, TR-010) doesn't say which permission mode or settings a managed session gets. It also doesn't say whether your normal harness config is loaded.
- "Expired request" (FR-017, TR-045) also needs a definition for each runtime. Some runtimes don't have native expiry.

### 5. Two people can control the same session at once

- FR-037 lets you open the native terminal for a session Robin manages.
- Nothing says what happens if you resume the session there while Robin still holds it, or if Robin and the terminal both send input.
- FR-013's "do not duplicate an active run" can't be enforced against outside resumes.
- **Fix:** add a control-lease or ownership rule, or document that the terminal handoff gives up Robin's control.

### 6. The scope is very large for a one-person MVP

- The MVP covers three full live-control adapters, history indexing, search, findings, lineage and loop detection, context packets locked by hash, SSE replay, the action ledger, migrations with backup, and export.
- TR-091 adds "do not silently reduce the three-runtime scope". That ties the release to the weakest integration, probably Pi through a Node sidecar.
- **Suggestions:**
  - Define a minimum set of capabilities each runtime must support to ship.
  - Allow release with two runtimes if Pi fails Phase 0.
  - Move FR-025 (loop prevention) to P1. In user-directed workflows, a "derived" label on the preview is enough.
  - Move TR-074 (export) to P1.

### 7. Hooks for observed sessions have no consent or install rules

- FR-041 ingests "supported hooks or logs for observed sessions".
- Installing hooks changes the user's harness config. That conflicts with "discovery is read-only" and has no rules for consent, install or uninstall.

### 8. Context sharing can carry prompt injection

- Findings and transcript excerpts move text from one agent into another. That text can include tool output from web pages or repo files.
- TR-060 and TR-070 only treat this text as untrusted inside the browser.
- **Fix:** frame packets as quoted data, and flag excerpts that contain tool or web output in the preview.

## Where the PRD and the technical document disagree

| Topic | PRD | Technical document |
|---|---|---|
| Storage location | `.robin/tasks/...` in the repo (§9). §18 recommends a global database plus `.robin` artifacts | App-data directory by default; `.robin` only as an explicit export (TR-021) |
| Finding schema | YAML with `id`; evidence as path anchors; no creation time or parents, though FR-020 requires creation time | JSON with no `id` or version field, though TR-022 requires immutable versions; evidence as `artifact_id` plus range; `parents` |
| Finding file format | `findings/*.md` | A structured schema |
| Attention items | Include "findings awaiting review" (FR-035) | AttentionItem requires a native request ID, which these items don't have |
| Traceability | FR-050–051 (Git metadata) | Mapped to TR-070–072 (security and lifecycle). No TR covers refreshing Git metadata |

Because the PRD's §10 restates the architecture and stack, the two documents have already drifted apart. I'd cut §10 down to a pointer to the technical document.

## Smaller items

- **The CLI surface doesn't match the examples.**
  - `robin synthesize --from codex,pi` (§7) is missing from Appendix A.
  - `share finding codex:find_01 --to claude:session_42 --prompt` doesn't match `share SOURCE --to TARGET`.
  - `--from codex` is ambiguous when a task has two sessions of the same runtime.
  - The CLI has no defined way to preview and confirm a share without an interactive prompt (for example a `--yes` flag), so FR-032's confirmation rule has no CLI path.
- **Some targets can't be measured:**
  - "Materially smaller" packets (§14).
  - "Primary interactions" is undefined. FR-030 asks for three, §14 asks for two.
  - The size of the search corpus for NFR-02.
  - The task status values are never listed (TR-040 only says "explicit task decision").
- **Success measures:** they are all acceptance tests. Consider one outcome measure, such as the time to get a cross-agent review compared with copy-paste.
- **tmux:** it's the terminal fallback (§10, risks), but TR-071's install checks don't look for it. Say what happens when it's missing.
- **Subscription logins:** TR-012 says not to assume subscription credentials work with programmatic integrations. That could change the cost model, so make it an explicit Phase 0 exit check for each runtime.
- **Secrets in the search index:** transcripts indexed for search can hold plaintext secrets from tool output. Consider scanning at index time as well as at share time.
- **Formatting:**
  - Headings have lost their punctuation ("Version 0 2 changes", "Goals non goals and principles").
  - Several items after bullet lists are plain paragraphs instead of bullets: §1 MVP, §7, §15, §19, §20, Appendix B. This could be a conversion artifact from pandoc, so check it in Word.
  - The technical document's map and references use page numbers, which break when the layout changes.

## Suggested next steps

1. Decide items 1–3 above, since they change the architecture.
2. Make one document the owner of the storage layout and the finding schema, and point the other to it.
3. Add tests for independence and concurrent control to the acceptance scenario.
4. Set the minimum set of capabilities each runtime needs to ship.
