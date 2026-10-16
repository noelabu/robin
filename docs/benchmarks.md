# Benchmarks (WP-4.4)

Targets come from the PRD's non-functional requirements (NFR-01–04, 09, 11) and TR-080. The reference
machine is the development MacBook (owner decision 2026-10-16).

## Dataset

`bench/dataset.py` builds a seeded dataset through Robin's normal ingest path and indexes it for search:
10,000 observed sessions across Claude Code, Codex and Pi, 25 projects, 20 events each (messages, tool calls
and tool output), 200,000 events in all.

```sh
uv run python bench/dataset.py /path/to/scratch-data --sessions 10000 --events 20 --seed 7
uv run python bench/run.py /path/to/scratch-data
```

Building it took 62 s to ingest and 97 s to index.

## Results — 2026-10-16 (release)

Same machine, Robin at `f91d964`. Dataset built in 146 s.

| Measure | Target | Result |
|---|---|---|
| Service startup to healthy (includes `uv run`) | ≤ 2 s | **0.48 s** |
| Workspace snapshot | — | 120 ms, 763 KB |
| Search, 100 two-word queries, `limit=50` | p95 ≤ 300 ms | p50 20.3 ms, **p95 21.4 ms** |
| Idle CPU, service + supervisor, 30 s | < 1% | **0.0%** |
| Live events, 20 concurrent sessions, 30 s | 95% in the UI within 2 s | **100%** within 2 s; p50 239 ms, p95 286 ms |
| Concurrent active sessions | 20 | 20 (387 events/s through the stream) |

## Results — 2026-10-15

Machine: Apple M4 Pro, 48 GB, macOS 26.5. Robin at `WP-4.4`.

| Measure | Target | Result |
|---|---|---|
| Service startup to healthy (includes `uv run`) | ≤ 2 s | **0.83 s** |
| Workspace snapshot (500 most recent observed sessions + managed ones) | — | 120 ms, 763 KB |
| Search, 100 two-word queries, `limit=50` | p95 ≤ 300 ms | p50 19.8 ms, **p95 23.7 ms** |
| Idle CPU, service + supervisor, 30 s | < 1% | **0.0%** |
| Live events, 20 concurrent sessions, 30 s | 95% in the UI within 2 s | **100%** within 2 s; p50 231 ms, p95 288 ms |
| Concurrent active sessions | 20 | 20 (389 events/s through the stream) |

Live latency is measured from the supervisor's timestamp on each event to its arrival on the dashboard's
event stream (`/api/v1/stream`), the same path the browser uses.

## What keeps these bounded

- The workspace snapshot carries every managed session and only the 500 most recently updated observed
  ones; older sessions load a page at a time from the oldest one already loaded (`/sessions?before_updated_at=&before_id=`), and the sidebar renders only the rows in
  view.
- The activity feed keeps at most 500 events in the browser and loads scope history in pages.
- Search is an FTS5 index updated incrementally off the request path; queries are capped at 100 results.
- Idle loops wait on change notifications with multi-second timeouts rather than polling tightly.
