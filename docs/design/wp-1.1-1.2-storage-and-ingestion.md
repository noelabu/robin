# WP-1.1 / WP-1.2: storage foundation and event ingestion

- **Status:** proposed, awaiting approval
- **Date:** 2026-10-15
- **Authority:** TRD v0.4 (TR-003, 004, 020–023, 030–033, 070, 073, 082), `docs/corpus.md`
- **Scope:** storage foundation, the event envelope, and both ingest paths (supervisor stream and JSONL tailer)
- **Out of scope:**
  - runtime parsers
  - supervisor productization (WP-1.3)
  - runtime manager and action-ledger behaviour (WP-1.4); their tables are created now
  - API, SSE and web
  - full-text search
  - user-facing backup and export

## 1. What gets built

```
pyproject.toml            # buildable package (hatchling, src layout); Python >= 3.13; dev: pytest, ruff, mypy
.python-version           # 3.13
src/robin/
  ids.py                  # new_id(), utc_now(), UTC text format
  paths.py                # data dir (ROBIN_DATA_DIR), ProjectRoot / ExecutionCwd / ArtifactStoreRoot, 0700/0600 helpers
  store/db.py             # writer + read-only engines, PRAGMAs, BEGIN IMMEDIATE, UTCDateTime
  store/models.py         # SQLAlchemy models (Alembic target)
  store/migrate.py        # compatibility check, online backup, transactional upgrade
  store/migrations/       # Alembic env + 0001_baseline
  store/blobs.py          # content-addressed blob store: atomic put, reconcile
  store/writer.py         # single writer thread, bounded queue, futures
  store/lifecycle.py      # open_store(): dirs, lock, migrate, reconcile, start writer
  events/envelope.py      # EvidenceKind, EventFamily, event types, NewEvent, EventEnvelope
  events/ingest.py        # ingest_batch(): spill large payloads, one transaction with the checkpoint
  events/tailer.py        # JsonlTailer.poll_once(), RecordMapper
  events/supervisor_source.py  # SupervisorIngestor
tests/                    # pytest only collects tests/ (the Phase 0 scripts in supervisor/ are excluded)
```

The `.venv` moves to Python 3.13. Under 3.12.0, the Phase 0 supervisor hung on shutdown in `asyncio.Server.wait_closed()`.

## 2. Data directory

```
~/Library/Application Support/Robin/        0700   (override: ROBIN_DATA_DIR)
  robin.db, robin.db-wal, robin.db-shm      0600   SQLite, WAL, foreign keys on, synchronous=FULL
  robin.lock                                0600   flock: one owner per data dir
  artifacts/sha256/ab/cd/<sha256>           0600   payloads > 64 KiB, quarantined raw bytes
  backups/robin-0001_baseline-20261015T051200Z.db   pre-migration backups (SQLite online backup API)
```

## 3. Schema (`0001_baseline`)

| Table | Key columns | Constraints that matter |
|---|---|---|
| `meta` | `key`, `value` | Holds `host_id`, created once |
| `session` | `session_id`, `host_id`, `runtime`, `native_session_id?`, `mode` (managed/observed), `execution_cwd?`, `capability_snapshot?`, `control_owner?`, `lease_generation` | **`UNIQUE(host_id, runtime, native_session_id)`**: native identity (TR-023). NULL until captured. |
| `run` | `run_id`, `session_id`, `native_turn_id?`, `started_at`, `ended_at?`, `outcome?` | `UNIQUE(session_id, native_turn_id)`. `task_id` arrives in Phase 2 via `ALTER TABLE ADD COLUMN … REFERENCES task`. |
| `connection` | `connection_id`, `session_id`, `connection_generation`, `supervisor_generation?`, `state`, `coverage?`, `cursor?`, timestamps | `UNIQUE(session_id, connection_generation)` |
| `source_checkpoint` | (`source_id`, `generation`), `kind` (supervisor/jsonl_file), `path?`, `file_dev?`, `file_ino?`, `offset`, `anchor_hash?`, `state` (active/superseded), `reason` (initial/rotated/truncated/rewritten) | One active generation per source (partial unique index) |
| `blob` | `sha256`, `size`, `media_type`, `state` (present/missing) | |
| `event` | `global_cursor` (AUTOINCREMENT), `event_id`, `session_id`, `runtime`, `native_session_id?`, `run_id?`, `source_id`, `source_generation`, `source_event_id?`, `source_seq`, `source_offset_end?`, `dedup_key`, `native_event_id?`, `duplicate_of?`, `source_time?`, `ingested_at`, `event_family`, `event_type`, `evidence_kind`, `payload_inline?` / `payload_blob?`, `payload_size`, `content_hash` | **`UNIQUE(source_id, dedup_key)`**: source-qualified event identity (TR-023). Exactly one of inline or blob. Cursors are never reused. |
| `quarantine` | `quarantine_id`, `source_id`, `generation`, `offset_start`, `offset_end`, `reason`, `diagnostic` (no content), `byte_length`, `content_hash?`, `payload_blob?` | `UNIQUE(source_id, generation, offset_start)` |
| `control_action` | `action_id` (client-generated), `session_id`, `action_type`, `request_hash`, `expected_generation?`, payload, `state` (requested → submitted → acknowledged/failed/cancelled/outcome_unknown), `native_correlation?`, timestamps | **PK = client action ID** (TR-023). Table only; behaviour comes in WP-1.4. |
| `audit_entry` | `audit_id`, `at`, `actor`, `action`, `affected_ids`, `outcome`, `correlation_id?`, `action_id?` | |

**`dedup_key`** is a contract with two forms:
- `n:<source_event_id>` when the source has a native identity
- `r:<generation>:<start>-<end>` (the byte range) otherwise

The content hash is never part of the key, so two identical messages stay two events (TR-032). An event without a native ID whose content hash matches one in an earlier generation of the same source is inserted with `duplicate_of` set. That is the "unresolved duplicate candidate" marker.

## 4. Envelope

```python
class EvidenceKind(StrEnum): reported, observed, inferred, unknown
class EventFamily(StrEnum):  session, message, tool, file, attention, finding, connection, unknown
# event_type: "<family>.<name>", e.g. "tool.started". Unknown types are accepted; their family comes from the prefix, or is "unknown".
```

**A committed event as read back.** This one came from the supervisor stream; the payload is canonical JSON, stored inline because it's 64 KiB or less:

```json
{
  "global_cursor": 4182,
  "event_id": "7d1c3f0e-5a7b-4f0c-9b8e-2f4c1a9e6b21",
  "schema_version": 1,
  "runtime": "claude",
  "session_id": "0887af91-1611-4c1e-a7f2-3b5d9c0e4a10",
  "native_session_id": "0e34bc78-2ce3-46b7-a7ac-5bffc8debcd9",
  "run_id": null,
  "task_id": null,
  "source_id": "supervisor:3f9a1c2b7d4e",
  "source_generation": 0,
  "source_event_id": "3f9a1c2b7d4e:57",
  "source_seq": 57,
  "source_time": "2026-10-15T04:26:11.482113Z",
  "ingested_at": "2026-10-15T04:26:11.490552Z",
  "event_family": "unknown",
  "event_type": "unknown.raw",
  "evidence_kind": "unknown",
  "payload_ref": null,
  "content_hash": "sha256:9b1f…e04c"
}
```

**The same kind of record from a tailed file whose payload was over 64 KiB.** The payload lives in the blob store, and the dedup key is a byte range:

```json
{
  "global_cursor": 4190,
  "source_id": "file:/Users/noelabu/.claude/projects/-Users-noelabu-Documents-Personal-robin/2a93….jsonl",
  "source_generation": 0,
  "source_event_id": null,
  "source_seq": 1048576,
  "source_offset_end": 1418235,
  "event_type": "unknown.raw",
  "payload_ref": "blob:sha256:41c7…aa09",
  "content_hash": "sha256:41c7…aa09"
}
```

Runtime parsers (WP-1.5 onward) turn `unknown.raw` into typed events such as `message.delta` and `tool.result` by supplying a mapper. The storage contract doesn't change.

## 5. Ingestion contract

```python
store = open_store()                               # dirs + lock + migrate (with backup) + reconcile + writer

# Supervisor stream (WP-1.3 will call this; tested now with recorded spike records)
sup = SupervisorIngestor(store)
results = sup.ingest([SupervisorRecord(seq=57, gen="3f9a1c2b7d4e", session=robin_session_id,
                                       at=1792030771.48, event={"type": "approval_request", ...})])
# -> [Inserted(event_id=..., global_cursor=4182, duplicate_of=None)]
sup.ingest(same_records)                           # -> [Duplicate(dedup_key="n:3f9a1c2b7d4e:57")]
ack_cursor = sup.checkpoint("3f9a1c2b7d4e")        # 57; acknowledge to the supervisor only after this

# JSONL file
tail = JsonlTailer(store, path, session_id=robin_session_id, runtime="claude")   # mapper=default_mapper
r = tail.poll_once()
# -> PollResult(inserted=812, duplicates=0, quarantined=1, generation=0, offset=3_145_982, pending_tail_bytes=211)
```

| Situation | Behaviour |
|---|---|
| Line still being written (no trailing LF) | Not emitted. Checkpoint stays at the start of that line. Picked up on the next poll. |
| Complete line that isn't JSON | Quarantined (`invalid_json`). Checkpoint advances past it in the same transaction. |
| Record over 16 MiB | Quarantined (`oversized`); raw bytes not kept. Checkpoint advances. Memory stays around 17 MiB however large the record. |
| File rotated (new inode) | Old file handle drained to EOF. A leftover partial line is quarantined (`incomplete_at_rotation`). A new generation starts at offset 0. History is kept. |
| File truncated or rewritten | New generation (`truncated` or `rewritten`, the latter detected by a hash of the bytes before the offset). History is kept. |
| U+2028 inside a JSON string, or CRLF | Records are split on LF only and a trailing CR is stripped, so one record stays one record. |
| Same record ingested twice | Returns `Duplicate`, not an error. |
| Crash anywhere | Events, quarantines and the checkpoint commit in one transaction. Re-reading from the old offset produces the same dedup keys, so nothing is stored twice. |
| Supervisor record for an unknown session | Quarantined (`unknown_session`, raw bytes kept); the stream continues. |
| Gap in supervisor seq | A `connection.gap` event (`observed`) is inserted in the same transaction. |

**Writer:** one thread owns the only write connection. Work items are SQL-only, and blob writes and fsyncs happen before submitting. Each batch is capped at 500 records or 4 MiB. A caller's future resolves only after COMMIT, so "commit before notify" (TR-004) holds by construction. When the queue is full:
- sync callers wait up to 5 s, then get `WriterBusy`
- async callers get `WriterBusy` at once, so they can slow their source

## 6. Migrations

`open_store()` does the following, in order:

1. Refuse a database whose revision this build doesn't know (`SchemaTooNew`, nothing written).
2. If an upgrade is due, take an online backup to `backups/`, run `quick_check` on it, and fsync it.
3. Run every pending revision in a single transaction, with a `foreign_key_check` before commit.
4. If the upgrade is interrupted, the database stays at the old revision, and the error names the backup.

## 7. Tests (pytest, `tests/` only)

~45 tests across:
- paths and permissions
- migrations: fresh, upgrade-with-backup, newer schema refused, interrupted migration rolled back, lock
- blobs: atomic, idempotent, reconcile
- writer: commit-then-result, rollback, backpressure, async, close
- envelope
- ingest:
  - duplicates
  - identical messages kept apart
  - the 64 KiB spill boundary
  - duplicate candidates
  - checkpoint conflict
- tailer:
  - partial tail
  - CRLF
  - U+2028
  - blank lines
  - malformed and oversized records
  - rotation, truncation and rewrite
  - bounded reads
- supervisor source: replay, per-generation checkpoints, unknown session, sequence gap
- crash: an in-process fault hook, and a subprocess killed with `os._exit` inside a transaction

## 8. Where this departs from the TRD

| TRD | This design | Follow-up |
|---|---|---|
| TR-082: 1 MiB parser limit | 16 MiB (corpus: 171 valid records over 1 MiB) | Update TRD; raise the spike's `MAX_LINE` in WP-1.3 |
| TR-003: Python 3.12+ | Python 3.13 | Update the plan's layout comment |
| TR-020: a run carries a task ID | No `task_id` until Phase 2 | Phase 2 adds it with an FK |
| TR-030: `native_session_id` required | Nullable: early supervisor events arrive before it's captured | None |
| TR-022: flush, then rename | Also fsyncs the file and its directory | None (stricter) |
| TR-033: incomplete tail stays pending | On a rotated-away file, quarantined as `incomplete_at_rotation` | None |
| TR-033: no lost history on rotation | Bytes appended to a rotated file while Robin wasn't running can't be recovered | Record as a gap; Phase 4 file watching |

## 9. Order of work

| Step | What | Size |
|---|---|---|
| 1 | Toolchain: Python 3.13, pyproject, `uv sync`; check the spike still runs | 0.5 d |
| 2 | `ids`, `paths` | 0.5 d |
| 3 | `db`, `models`, `0001_baseline` | 1.5 d |
| 4 | Migrations and lifecycle | 1 d |
| 5 | Blob store | 0.5 d |
| 6 | Writer. **Commit WP-1.1** after `/review-lite` | 1 d |
| 7 | Envelope | 0.5 d |
| 8 | Ingest | 1.5 d |
| 9 | Supervisor source | 0.5 d |
| 10 | Tailer | 2 d |
| 11 | Crash tests, `/schema-check`. **Commit WP-1.2** | 0.5 d |
