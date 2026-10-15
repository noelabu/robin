"""Benchmarks against a dataset from bench/dataset.py (WP-4.4: NFR-01–04, 09, 11; TR-080).

Measures service startup, the workspace snapshot, search latency, idle CPU, and live event latency with 20
concurrent scripted sessions, and prints one JSON result. Targets: startup <= 2 s, search p95 <= 300 ms,
idle CPU < 1%, 95% of live events reach the dashboard stream within 2 s.

usage: uv run python bench/run.py DATA_DIR [--sessions-live 20] [--live-s 30] [--idle-s 30]
"""

import argparse
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from dataset import WORDS  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else float("nan")


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not out:
        return 0.0
    parts = out.replace("-", ":").split(":")
    secs = 0.0
    for part in parts:
        secs = secs * 60 + float(part)
    return secs


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(data: Path, live_sessions: int, live_s: float, idle_s: float) -> dict:
    port = free_port()
    env = {**os.environ, "ROBIN_FAKE_TICK": "0.05"}
    started = time.monotonic()
    service = subprocess.Popen(
        ["uv", "run", "robin", "serve", "--data", str(data), "--port", str(port), "--no-browser"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    result: dict = {"dataset": str(data)}
    try:
        while not (data / "service.json").exists():
            time.sleep(0.02)
        token = (data / "cli.token").read_text().strip()
        c = httpx.Client(
            base_url=f"http://127.0.0.1:{port}/api/v1",
            headers={"authorization": f"Bearer {token}"},
            timeout=60,
        )
        while True:
            try:
                if c.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.02)
        result["startup_s"] = round(time.monotonic() - started, 2)  # includes `uv run` start-up

        t = time.monotonic()
        ws = c.get("/workspace")
        result["workspace"] = {
            "ms": round((time.monotonic() - t) * 1000),
            "bytes": len(ws.content),
            "sessions_listed": len(ws.json()["sessions"]),
        }

        rng = random.Random(11)
        lat = []
        for _ in range(100):
            q = " ".join(rng.sample(WORDS, 2))
            t = time.monotonic()
            c.get("/search", params={"q": q, "limit": 50})
            lat.append((time.monotonic() - t) * 1000)
        result["search_ms"] = {"p50": round(pct(lat, 0.5), 1), "p95": round(pct(lat, 0.95), 1)}

        while c.get("/health").json()["checks"]["index"]["backlog_events"]:
            time.sleep(1)
        pids = [service.pid] + [
            int(p)
            for p in subprocess.run(
                ["pgrep", "-f", f"--data {data}"], capture_output=True, text=True
            ).stdout.split()
        ]
        time.sleep(3)
        before = sum(cpu_seconds(p) for p in pids)
        time.sleep(idle_s)
        result["idle_cpu_percent"] = round(100 * (sum(cpu_seconds(p) for p in pids) - before) / idle_s, 2)

        result["live"] = live(c, port, token, data, live_sessions, live_s)
    finally:
        service.terminate()
        service.wait(15)
        subprocess.run(["pkill", "-f", f"robin.supervisor.server --data {data}"], capture_output=True)
    return result


def live(c: httpx.Client, port: int, token: str, data: Path, n: int, seconds: float) -> dict:
    """n scripted sessions emitting a tick every 50 ms; latency from the supervisor's timestamp to the
    event arriving on the dashboard's stream."""
    cwd = data.parent / f"live-{uuid.uuid4().hex[:6]}"
    cwd.mkdir()
    head = c.get("/workspace").json()["watermark"]
    latencies: list[float] = []
    stop = threading.Event()

    def listen() -> None:
        with (
            httpx.Client(timeout=None) as s,
            s.stream(
                "GET",
                f"http://127.0.0.1:{port}/api/v1/stream?after={head}",
                headers={"authorization": f"Bearer {token}"},
            ) as r,
        ):
            for line in r.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("data: "):
                    ev = json.loads(line[6:]).get("event", {})
                    if ev.get("source_time") and ev.get("event_type") == "unknown.raw":  # the ticks
                        sent = datetime.fromisoformat(ev["source_time"])
                        latencies.append((datetime.now(UTC) - sent).total_seconds())

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    sids = [
        c.post(
            "/sessions/launch",
            json={
                "runtime": "fake",
                "cwd": str(cwd),
                "prompt": "go",
                "action_id": f"bench-{uuid.uuid4().hex}",
            },
        ).json()["session_id"]
        for _ in range(n)
    ]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:  # the scripted runtime asks for one approval per turn
        for item in c.get("/attention").json()["items"]:
            if item["kind"] == "approval":
                c.post(
                    f"/attention/{item['attention_id']}/responses",
                    json={"decision": "allow", "action_id": f"bench-{uuid.uuid4().hex}"},
                )
        time.sleep(0.5)
    stop.set()
    for sid in sids:
        c.post(f"/sessions/{sid}/actions", json={"type": "stop", "action_id": f"bench-{uuid.uuid4().hex}"})
    within = sum(1 for x in latencies if x <= 2.0)
    return {
        "sessions": n,
        "events": len(latencies),
        "events_per_s": round(len(latencies) / seconds, 1),
        "p50_s": round(pct(latencies, 0.5), 3),
        "p95_s": round(pct(latencies, 0.95), 3),
        "within_2s": round(within / len(latencies), 4) if latencies else None,
        "median_ms": round(statistics.median(latencies) * 1000, 1) if latencies else None,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--sessions-live", type=int, default=20)
    ap.add_argument("--live-s", type=float, default=30)
    ap.add_argument("--idle-s", type=float, default=30)
    a = ap.parse_args()
    print(json.dumps(run(a.data_dir, a.sessions_live, a.live_s, a.idle_s), indent=2))
