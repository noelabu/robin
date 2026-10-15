"""A reproducible benchmark dataset (WP-4.4, NFR-01–04): N observed sessions across three runtimes and a few
projects, each with a short transcript of messages and tool calls, ingested through the normal store path
and then indexed for search. Seeded, so the same arguments always produce the same data.

usage: uv run python bench/dataset.py DATA_DIR [--sessions 10000] [--events 20] [--seed 7]
"""

import argparse
import json
import random
import time
from pathlib import Path

from robin import search
from robin.events.envelope import EvidenceKind, NewEvent
from robin.events.ingest import CheckpointAdvance, ensure_session, ingest_batch, open_generation
from robin.store import open_store

WORDS = (
    "refactor parser cache token retry timeout migration schema index review bug flaky test deploy rollback "
    "latency memory leak thread lock queue session auth login payment invoice export import render layout "
    "button form validation error warning config secret rotate audit trace span metric alert"
).split()
TYPES = ["message.user", "message.completed", "tool.started", "tool.result"]


def sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def build(data_dir: Path, sessions: int, events: int, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    store = open_store(data_dir)
    started = time.monotonic()
    try:
        for i in range(sessions):
            runtime = ("claude", "codex", "pi")[i % 3]
            sid = ensure_session(
                store,
                runtime=runtime,
                mode="observed",
                native_session_id=f"bench-{i:06d}",
                execution_cwd=f"/Users/bench/project-{i % 25}",
            )
            source = f"bench:{i}"
            store.writer.call(
                lambda c, s=source, r=runtime, x=sid: open_generation(
                    c, source_id=s, kind="jsonl_file", runtime=r, reason="initial", session_id=x
                )
            )
            batch = []
            for n in range(1, events + 1):
                kind = TYPES[n % len(TYPES)]
                payload = (
                    {"text": sentence(rng, rng.randint(5, 40))}
                    if kind.startswith("message")
                    else {
                        "tool": "shell",
                        "input": {"command": sentence(rng, 3)},
                        "output": sentence(rng, 20),
                    }
                )
                batch.append(
                    NewEvent(
                        runtime=runtime,
                        session_id=sid,
                        source_id=source,
                        source_generation=0,
                        source_event_id=f"{i}-{n}",
                        source_seq=n,
                        event_type=kind,
                        evidence_kind=EvidenceKind.REPORTED,
                        payload=json.dumps(payload).encode(),
                    )
                )
            ingest_batch(store, batch, [], CheckpointAdvance(source, 0, events, None))
        ingested = time.monotonic() - started
        while search.catch_up(store, max_batches=200):
            pass
        return {
            "sessions": sessions,
            "events": sessions * events,
            "ingest_s": round(ingested, 1),
            "index_s": round(time.monotonic() - started - ingested, 1),
        }
    finally:
        store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--sessions", type=int, default=10_000)
    ap.add_argument("--events", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    print(json.dumps(build(a.data_dir, a.sessions, a.events, a.seed)))
