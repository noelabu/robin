"""A diagnostic bundle the user can look at before writing it anywhere (TR-083, TR-084).

It holds versions, doctor checks, health, counts, settings and the recent log, all redacted with the secret
scanner. It never holds prompts, messages, transcripts, findings, credentials or tokens. `preview()` shows
exactly what would be written and which redactions were applied.
"""

import json
import platform
import sqlite3
import sys
import tomllib
from pathlib import Path
from typing import Any

from robin import __version__
from robin.redact.scanner import for_data_dir

LOG_LINES = 200


def _counts(data_dir: Path) -> dict[str, Any]:
    db = data_dir / "robin.db"
    if not db.exists():
        return {}
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        return {
            "sessions": {
                f"{r}/{m}": n for r, m, n in q("SELECT runtime, mode, count(*) FROM session GROUP BY 1, 2")
            },
            "events": q("SELECT count(*) FROM event")[0][0],
            "tasks": q("SELECT count(*) FROM task")[0][0],
            "artifacts": {k: n for k, n in q("SELECT kind, count(*) FROM artifact GROUP BY 1")},
            "quarantined": q("SELECT count(*) FROM quarantine")[0][0],
            "schema": q("SELECT version_num FROM alembic_version")[0][0],
        }


def _settings(data_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("settings.toml", "index.toml"):
        path = data_dir / name
        if path.is_file():
            out[name] = path.read_text()
    custom = data_dir / "redact.toml"
    if custom.is_file():  # rule IDs only: the patterns themselves can describe secrets
        rules = tomllib.loads(custom.read_text()).get("rules", [])
        out["redact.toml"] = {"custom_rule_ids": [r.get("id") for r in rules]}
    return out


def _log_tail(data_dir: Path) -> list[str]:
    path = data_dir / "logs" / "robin.log"
    if not path.is_file():
        return []
    return path.read_text(errors="replace").splitlines()[-LOG_LINES:]


def collect(
    data_dir: Path, doctor: list[tuple[str, str, str]], health: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "robin": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "doctor": [{"check": n, "status": s, "detail": d} for n, s, d in doctor],
        "health": health,
        "counts": _counts(data_dir),
        "settings": _settings(data_dir),
        "log_tail": _log_tail(data_dir),
    }


def preview(data_dir: Path, bundle: dict[str, Any]) -> tuple[str, list[str]]:
    """The exact text that would be written, and the rules that redacted something in it."""
    text = json.dumps(bundle, indent=2, ensure_ascii=False, default=str)
    clean, matches = for_data_dir(data_dir).redact(text)
    return clean, sorted({m.rule_id for m in matches})
