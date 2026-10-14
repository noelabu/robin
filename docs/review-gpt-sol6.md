# Robin PRD and technical requirements review

Date: October 14, 2026

Sources: [Robin PRD v0.2](Robin_PRD.docx) and [Technical Requirements v0.1](Robin_Technical_Requirements.docx).

**Verdict: Approve for Phase 0 validation, not yet for broad implementation.** The documents are well aligned, but unresolved issues concern context boundaries, session ownership, and the findings-publication workflow.

Both documents were reviewed in full for content and implementation readiness. Document layout and the proposed harness integrations were not validated. Neither source document was modified.

## What is already strong

The documents clearly separate Robin’s responsibilities from the native runtimes. They handle durable action IDs, uncertain delivery outcomes, stale connections, event replay, explicit context previews, and the distinction between an ended turn and completed work thoughtfully. The phased delivery plan is sensible.

## Findings ordered by priority

### R-01 High — Independent review lacks a defined context boundary

**References:** PRD FR-032, §9 task directory; TR-064, TR-066, TR-070.

Independent reviewers must not receive other agents’ findings before an explicit transfer. However, the PRD places findings together under a shared task directory, and the technical specification scopes MCP access without defining what reviewers can read through their native filesystem tools.

A reviewer could encounter another reviewer’s output in the shared directory even though Robin never sent a context packet. Same-user file permissions do not distinguish the agents.

**Proposed decision:** Explicitly distinguish “Robin does not inject unselected findings” from enforced isolation. If actual isolation is required, define the runtime-supported filesystem boundary and keep other reviewers’ outputs outside it.

**Acceptance test:** Publish a finding from reviewer A while reviewer B is running; verify the promised access boundary before and after an approved transfer.

### R-02 High — Reusing a native session across tasks conflicts with the context-sharing promise

**References:** TR-020; technical document’s open decision on task switching; PRD FR-023 and FR-032.

TR-020 allows a session to appear in multiple task histories and assigns each new run a current task. Changing that association does not remove earlier conversation context from the native session. A session moved from task A to task B may already know A’s material without any new Robin transfer.

This also leaves attribution unclear for queued messages, delayed tool results, and findings published during a task switch.

**Proposed decision:** Default execution on a different task to a fresh native session. Historical linking can remain many-to-many. If continuation is allowed, disclose retained context and bind task attribution to the run when it starts.

**Acceptance test:** Queue input, change the task association, and receive late events; verify both context disclosure and stable task attribution.

### R-03 High — Native-terminal fallback needs an ownership-transfer contract

**References:** PRD FR-013, FR-017, FR-037; TR-010–012, TR-042, TR-044.

The documents require a valid terminal fallback, but do not distinguish attaching to an existing process from resuming the same session in another process. Those have different implications for concurrent control, pending approvals, and history writes.

Robin’s per-session action serialization does not govern commands issued independently through a native terminal.

**Proposed decision:** Define each adapter’s terminal behavior as attach, controlled handoff, or unavailable while managed execution is active. Specify when Robin disables controls, who owns pending requests, and how control returns.

**Acceptance test:** Open the native interface during an active turn and during a pending approval; prove there is no second active owner or duplicate response.

### R-04 High — Finding publication is mandatory, but its non-MCP delivery path is unspecified

**References:** PRD FR-020, FR-024, §18; TR-060, TR-064, TR-066.

The core workflow requires agents to publish structured findings and save a synthesis. MCP is optional, while “files and adapter prompts” remains an open decision. The specification does not establish how Robin discovers a completed output, validates it, derives trusted author attribution, or handles malformed and duplicate submissions.

There is also a narrower schema mismatch: the PRD permits user-authored findings, but TR-060 requires an author runtime and native session.

**Proposed decision:** Specify one required publication path for every adapter, with an author union for user versus agent, immutable versions, submission identity, validation failures, and explicit synthesis-to-manifest linkage.

**Acceptance test:** With MCP disabled, collect one finding from every runtime, reject an invalid submission visibly, and save a synthesis linked to the approved inputs.

### R-05 Medium — Task storage and execution working directory are conflated

**References:** PRD FR-012, §9; TR-021 and TR-050.

FR-012 launches sessions in the task directory or worktree. TR-021 proposes storing task artifacts in the operating system’s application-data directory, while worktrees are optional. Following both literally can launch a coding session outside its project.

**Proposed decision:** Separate `project_root`, `execution_cwd`, and `artifact_store`. Define the default execution directory and how approved task inputs become available there.

**Acceptance test:** Launch against an ordinary directory and a Git repository without a worktree; verify the intended working directory and input availability.

### R-06 Medium — Agent retrieval grants lack a lifecycle and audit contract

**References:** PRD FR-022–023, §11–12; TR-063–066.

“Task-scoped,” “granted sources,” and “approved ranges” establish the intended policy, but there is no defined grant record, issuance flow, revocation behavior, or immutable-version binding. It is also unclear how an agent pulling findings through MCP satisfies the promise that every transfer has a manifest.

**Proposed decision:** Bind grants to the calling session, task, exact artifact versions or excerpt ranges, and approving action. Define revocation and record the actual content returned.

**Acceptance test:** An old grant must not expose a newly edited finding, a wider excerpt, or content from another task.

### R-07 Medium — Malformed-record handling can conflict with checkpoint advancement

**References:** TR-033, TR-082; PRD FR-010 and FR-041.

TR-033 requires quarantining malformed records but prohibits advancing beyond uncommitted accepted records. It does not say how a rejected complete record becomes a durable, safely skipped position. An implementation could repeatedly encounter the same bad record and never index valid records after it.

**Proposed decision:** Commit a rejection marker and checkpoint together for complete malformed records. Keep incomplete trailing records pending until complete.

**Acceptance test:** Process valid → malformed → valid records, restart repeatedly, and verify the final valid record appears exactly once.

## Recommended next step

Resolve R-01 through R-04 before finalizing the implementation contracts. Phase 0 should then produce the actual three-runtime capability matrix, especially for native-session resumption, terminal ownership, authentication, and approval handling. The documents correctly identify these as unvalidated; they should remain explicit release dependencies.

Possible follow-up work:

- Revise both documents to address these seven findings, distinguishing proposed product decisions from agreed requirements.
- Create a concrete Phase 0 validation plan covering terminal ownership, authentication, session resumption, approval handling, and finding publication.
- Specify session ownership, task context, finding publication, and context retrieval grant contracts.
