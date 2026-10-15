# Session corpus (Phase 0, WP-0.3)

- **Scanned:** 2026-10-15 04:52 UTC, on the development MacBook, by `corpus/inventory.py`
- **Method:** read-only, metadata only. Record types, version fields and JSON validity were read; no message content is in this report.
- **Excluded:** sessions created by Robin's own Phase 0 tests (temp folders named `robin-*`), counted in the last row.
- **Use:** the per-runtime session count is the denominator for the PRD §14 session-recovery target (90% openable and, where supported, resumable).

| | Claude Code | Codex | Pi |
|---|---|---|---|
| Store | `~/.claude/projects` | `~/.codex/sessions` | `~/.pi/agent/sessions` |
| Sessions | 839 | 283 | 7 |
| Projects (distinct working folders) | 108 | 28 | 4 |
| Total size | 3.7 GB | 291.3 MB | 1.2 MB |
| Session size p50 / p95 / max | 240.2 KB / 3.6 MB / 1.1 GB | 598.5 KB / 3.0 MB / 23.9 MB | 74.9 KB / 768.9 KB / 768.9 KB |
| Records (JSONL lines) | 208,430 | 71,939 | 200 |
| Malformed lines (files affected) | 0 (0) | 0 (0) | 0 (0) |
| Files ending in an incomplete line | 0 | 0 | 0 |
| Date range (file modified) | 2026-09-15 → 2026-10-15 | 2026-02-09 → 2026-10-14 | 2026-08-13 → 2026-10-14 |
| Format / CLI versions seen | 2.1.295 (105), 2.1.281 (99), 2.1.299 (85), (none) (77), 2.1.296 (71), 2.1.278 (51), 2.1.286 (44), 2.1.294 (41) | 0.154.0 (72), 0.153.4 (66), 0.145.0-alpha.18 (55), 0.147.0 (26), 0.155.1 (13), 0.98.0 (10), 0.153.2 (9), 0.156.1 (7) | 3 (7) |
| Most common record types | `attachment` (99,933), `assistant` (46,725), `user` (29,460), `last-prompt` (4,800), `mode` (4,773), `atis-latch` (4,763) | `event_msg` (31,295), `response_item` (30,947), `token_usage_record` (5,788), `inter_agent_communication_metadata` (1,393), `turn_context` (1,113), `world_state` (967) | `message` (178), `model_change` (8), `session` (7), `thinking_level_change` (7) |
| Unreadable files | 0 | 0 | 0 |
| Robin test sessions excluded | 11 | 18 | 0 |

Re-run on the Mac mini M4 for the recorded corpus. Anonymized fixtures for adapter tests are chosen from this corpus separately (PRD §20).

## Observations (2026-10-15)

- **Claude Code keeps about 30 days of history.** The oldest Claude file is from 2026-09-15, and `cleanupPeriodDays` isn't set, so Claude Code's default cleanup applies. Robin's normalized copy will often be the only long-term record of a Claude session. That matters for retention (TR-081) and the 20 GB cap. Codex history goes back to February; Pi's is small.
- **Records larger than the proposed 1 MiB parser limit are real.** 164 Claude records (largest 1.3 MiB) in 22 files, and 7 Codex records (largest 3.9 MiB) in 3 files. TR-082's 1 MiB limit would reject valid records. Proposed change: a parser limit of 16 MiB per record. Records above the 64 KiB display limit keep their payload in artifact storage with a truncated preview; only records above the parser limit are rejected with a diagnostic.
- **Session files can be very large.** One Claude session is 1.1 GB (10,114 records), so indexing must stream and resume from a byte offset (TR-033). Reading whole files is not an option.
- **Claude subagent transcripts have their own format.** 76 of the 77 unversioned Claude files are under `subagents/`, with record types `launched`, `started`, `result`. The Claude adapter must parse them and link them to the parent session.
- **Format churn is high.** About 20 Claude Code versions appear in 30 days, and Codex spans 0.98 to 0.162. Fixtures need to cover a range of versions, and unknown record types must be tolerated (FR-011).
- **The stores were healthy at scan time:** no malformed lines and no incomplete tails. Live-write and partial-tail handling still needs fixture tests (NFR-03).
- **Robin's Phase 0 tests added sessions to the owner's history:** 11 Claude and 18 Codex sessions with `robin-*` temp working folders. Future tests should use a separate Claude config dir or Codex home, or ephemeral threads where possible.
