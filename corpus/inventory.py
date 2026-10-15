"""Phase 0: metadata-only inventory of the owner's native session stores (WP-0.3).

    uv run python corpus/inventory.py            # writes docs/corpus.md

Reads Claude Code, Codex and Pi session files read-only and records counts,
sizes, date ranges, format versions and parse health. No message content is
read into the report: only record types, version fields and JSON validity.
Sessions created by Robin's own Phase 0 tests (temp working folders named
robin-*) are counted separately.
"""

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
STORES = {
    "claude": [HOME / ".claude" / "projects"],
    "codex": [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"],
    "pi": [HOME / ".pi" / "agent" / "sessions"],
}
ROBIN_TEST = re.compile(r"robin-[a-z]+-[a-z-]*cwd|robin-(claude|codex|pi|pub|cont|iso|attach|daemon)")
REPORT = Path(__file__).resolve().parent.parent / "docs" / "corpus.md"


def scan_file(path: Path) -> dict:
    """Parse health and metadata for one JSONL session file, without keeping content."""
    info = {"bytes": path.stat().st_size, "mtime": path.stat().st_mtime, "lines": 0, "bad": 0,
            "incomplete_tail": False, "types": Counter(), "versions": set(), "cwd": None}
    with open(path, "rb") as f:
        data = f.read()
    if data and not data.endswith(b"\n"):
        info["incomplete_tail"] = True
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        info["lines"] += 1
        try:
            rec = json.loads(raw)
        except ValueError:
            info["bad"] += 1
            continue
        if not isinstance(rec, dict):
            continue
        info["types"][str(rec.get("type"))] += 1
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        for v in (rec.get("version"), payload.get("cli_version")):
            if isinstance(v, (str, int)):
                info["versions"].add(str(v))
        cwd = rec.get("cwd") or payload.get("cwd")
        if isinstance(cwd, str) and not info["cwd"]:
            info["cwd"] = cwd
    return info


def scan(runtime: str) -> dict:
    files = [p for root in STORES[runtime] if root.exists() for p in root.rglob("*.jsonl")]
    out = {"roots": [str(r).replace(str(HOME), "~") for r in STORES[runtime] if r.exists()],
           "sessions": 0, "test_sessions": 0, "bytes": 0, "lines": 0, "bad_lines": 0, "bad_files": 0,
           "incomplete_tails": 0, "versions": Counter(), "types": Counter(), "projects": set(),
           "sizes": [], "first": None, "last": None, "unreadable": 0}
    for p in files:
        try:
            info = scan_file(p)
        except OSError:
            out["unreadable"] += 1
            continue
        if ROBIN_TEST.search(str(p)) or (info["cwd"] and ROBIN_TEST.search(info["cwd"])):
            out["test_sessions"] += 1
            continue
        out["sessions"] += 1
        out["bytes"] += info["bytes"]
        out["lines"] += info["lines"]
        out["bad_lines"] += info["bad"]
        out["bad_files"] += bool(info["bad"])
        out["incomplete_tails"] += info["incomplete_tail"]
        out["versions"].update(info["versions"] or {"(none)"})
        out["types"].update(info["types"])
        out["projects"].add(info["cwd"] or p.parent.name)
        out["sizes"].append(info["bytes"])
        t = info["mtime"]
        out["first"] = t if out["first"] is None else min(out["first"], t)
        out["last"] = t if out["last"] is None else max(out["last"], t)
    return out


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pct(sorted_sizes, q):
    return sorted_sizes[min(len(sorted_sizes) - 1, int(q * len(sorted_sizes)))] if sorted_sizes else 0


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "-"


def main():
    results = {rt: scan(rt) for rt in STORES}
    lines = [
        "# Session corpus (Phase 0, WP-0.3)",
        "",
        f"- **Scanned:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, on the development MacBook, by `corpus/inventory.py`",
        "- **Method:** read-only, metadata only. Record types, version fields and JSON validity were read; no message content is in this report.",
        "- **Excluded:** sessions created by Robin's own Phase 0 tests (temp folders named `robin-*`), counted in the last row.",
        "- **Use:** the per-runtime session count is the denominator for the PRD §14 session-recovery target (90% openable and, where supported, resumable).",
        "",
        "| | Claude Code | Codex | Pi |",
        "|---|---|---|---|",
    ]
    rows = [
        ("Store", lambda r: ", ".join(f"`{x}`" for x in r["roots"]) or "not found"),
        ("Sessions", lambda r: f"{r['sessions']:,}"),
        ("Projects (distinct working folders)", lambda r: f"{len(r['projects']):,}"),
        ("Total size", lambda r: human(r["bytes"])),
        ("Session size p50 / p95 / max", lambda r: " / ".join(human(pct(sorted(r['sizes']), q)) for q in (0.5, 0.95, 1.0))),
        ("Records (JSONL lines)", lambda r: f"{r['lines']:,}"),
        ("Malformed lines (files affected)", lambda r: f"{r['bad_lines']:,} ({r['bad_files']:,})"),
        ("Files ending in an incomplete line", lambda r: f"{r['incomplete_tails']:,}"),
        ("Date range (file modified)", lambda r: f"{day(r['first'])} → {day(r['last'])}"),
        ("Format / CLI versions seen", lambda r: ", ".join(f"{v} ({n})" for v, n in r["versions"].most_common(8))),
        ("Most common record types", lambda r: ", ".join(f"`{t}` ({n:,})" for t, n in r["types"].most_common(6))),
        ("Unreadable files", lambda r: f"{r['unreadable']:,}"),
        ("Robin test sessions excluded", lambda r: f"{r['test_sessions']:,}"),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(results[rt]) for rt in STORES) + " |")
    lines += ["", "Re-run on the Mac mini M4 for the recorded corpus. Anonymized fixtures for adapter tests are chosen from this corpus separately (PRD §20)."]
    notes = Path(__file__).resolve().parent / "notes.md"
    if notes.exists():  # hand-written observations, kept across re-runs
        lines += ["", notes.read_text().rstrip()]
    REPORT.write_text("\n".join(lines) + "\n")
    print(REPORT.read_text())


if __name__ == "__main__":
    main()
