"""Packaging (TR-071): the wheel carries the built dashboard and data files, installs into a clean venv, and
runs. Needs ROBIN_PACKAGING=1 (it builds and installs, so it takes a while)."""

import json
import os
import socket
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ROBIN_PACKAGING") != "1", reason="set ROBIN_PACKAGING=1 to run"
)
ROOT = Path(__file__).resolve().parents[2]
NEEDED = [
    "robin/web/dist/index.html",
    "robin/redact/gitleaks/gitleaks.toml",
    "robin/redact/gitleaks/LICENSE",
    "robin/adapters/pi/robin_extension.ts",
    "robin/hooks/pi_observer.ts",
    "robin/findings/mcp_server.py",
    "robin/store/migrations/env.py",
]


def test_wheel_installs_and_runs(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path), "-q"], cwd=ROOT, check=True)
    (wheel,) = tmp_path.glob("robin-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert [n for n in NEEDED if n not in names] == []
    migrations = sorted(n for n in names if "/migrations/versions/" in n and n.endswith(".py"))
    assert len(migrations) == len(list((ROOT / "src/robin/store/migrations/versions").glob("0*.py")))

    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "-q", "-p", "3.13", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin/python"), str(wheel)], check=True
    )
    robin = str(venv / "bin/robin")
    data = tmp_path / "data"
    doctor = subprocess.run([robin, "doctor", "--data", str(data)], capture_output=True, text=True)
    assert "FAIL" not in doctor.stdout, doctor.stdout

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    service = subprocess.Popen(
        [robin, "serve", "--data", str(data), "--port", str(port), "--no-browser"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while not (data / "service.json").exists() and time.time() < deadline:
            time.sleep(0.25)
        token = (data / "cli.token").read_text().strip()
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"authorization": f"Bearer {token}"})
        while time.time() < deadline and not c.get("/api/v1/health").json().get("supervisor_connected"):
            time.sleep(0.25)
        health = c.get("/api/v1/health").json()
        assert health["ready"] and health["supervisor_connected"], json.dumps(health)
        assert c.get("/").status_code == 200
    finally:
        # The service restarts a stopped supervisor, so stop the service first, then its supervisor.
        service.terminate()
        service.wait(10)
        subprocess.run(["pkill", "-f", f"robin.supervisor.server --data {data}"], capture_output=True)
