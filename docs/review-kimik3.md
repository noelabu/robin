# Review: Robin PRD v0.2 & Technical Requirements v0.1

**Reviewer:** Kimi K3
**Date:** October 14, 2026
**Source documents:** `docs/Robin_PRD.docx` (v0.2), `docs/Robin_Technical_Requirements.docx` (v0.1)

## Overall verdict

These are unusually strong documents. The "honest state" philosophy (turn-end ≠ task success, unknown ≠ idle), idempotent action ledger, capability-gated controls, and lineage-based loop prevention are exactly the right hard problems to nail down early, and both docs do. The TRD correctly positions its stack/API choices as *proposed* while keeping safety semantics as *musts*. Below are the issues worth fixing before implementation.

---

## Cross-document inconsistencies (highest value to fix)

1. **MCP tool list mismatch.** PRD §10 lists 7 MCP tools including `session.search`. TRD TR-066 enumerates only 6 and omits `session.search`. Either add it to TR-066 or explicitly scope it out.

2. **Finding evidence format mismatch.** PRD §9's finding schema example uses plain path strings (`evidence: ["docs/PRD.md#authentication"]`); TRD TR-060 uses structured objects (`{"artifact_id": "src_08", "range": "120-135"}`). Align on the structured form — it's needed for immutable manifest hashing and excerpt bounds.

3. **Integration reference drift.** PRD Appendix C cites the Codex *blog post*, Claude *headless* docs, and Pi *SDK* docs; TRD cites Codex *app-server* docs, Claude *Agent SDK overview*, and Pi *RPC* docs. The PRD body text actually matches the TRD ("Codex app-server… Claude Agent SDK… Pi RPC or its SDK"). Update Appendix C to the canonical references, or list both with a stated purpose for each.

4. **Delivery states.** PRD §11 lists manifest delivery as "prepared, submitted, acknowledged, or failed"; TRD TR-064 adds `outcome_unknown`. The PRD should acknowledge the uncertain state — it's central to both docs' no-blind-retry stance.

5. **`synthesize` CLI ambiguity.** PRD §7 shows `robin synthesize --task prd-review --agent claude --from codex,pi`, but Appendix A shows `robin synthesize --task TASK --agent NAME` with no `--from`. Also, FR-024 says the user selects *findings*, not agents — the CLI should probably accept finding IDs, consistent with the context-preview model.

---

## PRD-specific issues

- **FR-001 / FR-003 acceptance criteria** mention "repository remote, current branch" even though FR-001 explicitly allows registering an "ordinary directory." Add "when applicable" or split the criteria.
- **"Session recovery" success measure (§14):** "at least 90% of sessions from validated agent versions" has no defined corpus or denominator. Specify the measured fixture/sample set.
- **FR-014 acceptance** says "Track task completion separately" but doesn't state *who* completes a task. TRD TR-040 says "explicit task decision with recorded evidence" — the PRD should say the same so the contract is testable at the product level.
- **NFR-01** conflates startup latency with corpus size ("interactive within 2 seconds for 10,000 indexed sessions"). TR-080 untangles this; consider tightening the PRD wording.
- **Formatting check in the .docx:** the last MVP bullet ("Show live activity, actual session counts…") appears detached from its list, and several FR table headers show merged "P0" cells inconsistently (e.g., FR-014 vs FR-015). Worth a visual pass in Word.

---

## TRD-specific issues

- **Fragile cross-references.** The document map and "References appear on page 12" rely on page numbers. In a living engineering doc, use section anchors — pages will drift on first edit.
- **Bootstrap secret hardening (TR-051).** The single-use URL-fragment secret flow is good, but specify: minimum entropy, expiry window, that failed exchanges invalidate the secret, and rate limiting on the exchange endpoint. The hostile-website threat model (TR-070) justifies this explicitly.
- **CLI surface has no contract.** PRD Appendix A defines ~15 CLI commands; TRD only covers CLI *authentication* (TR-052). A short command→endpoint mapping table would close the loop and prevent CLI/API drift.
- **Payload-limit relationships.** TR-062's 256 KiB packet limit vs TR-082's 1 MiB parser / 64 KiB inline-display limits are individually fine, but state how they interact (e.g., can a packet include an artifact reference to a >256 KiB payload, or only inline text?).
- **Traceability is one-directional.** The PRD→TR table is complete, but a reverse pass shows TR-073/074 (migrations, backup/export) map only to launch-checklist bullets. Fine, but consider promoting backup/export to a named FR — it's release-blocking per the checklist anyway.

---

## Gaps worth considering (both docs)

1. **Multi-tab coordination:** TR-090 tests "two browser tabs," but neither doc states the expected UX when two tabs submit conflicting controls on the same session (beyond action-ID dedup). One line in TR-042 would settle it.
2. **Adapter failure UX:** NFR-06 covers degraded adapters technically; the PRD could note the user-facing story (dashboard warning state, `robin doctor` guidance).
3. **Notification scope:** FR-033 is P1, but the attention queue is P0 — worth one sentence confirming the in-app queue is the P0 delivery mechanism so Phase 2 scope doesn't creep.

---

## What not to change

The phased plan, the "fixtures alone don't prove compatibility" gate (Appendix B / TR-090), the no-auto-approval stance, and the explicit `outcome_unknown` handling are all excellent — resist any pressure to soften them during Phase 0 when integration reality bites.
