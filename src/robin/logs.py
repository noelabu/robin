"""Structured, redacted logs (TR-083).

One JSON object per line in `<data dir>/logs/robin.log` (rotated): time, level, logger, message, and the
structured fields Robin passes with `extra=` (adapter, version, session ID, action ID, cursor, stage,
duration, safe error code). Every message and field goes through the secret scanner first, and code never
logs prompts, message text or credentials in the first place.
"""

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

from robin.paths import secure_mkdir

FIELDS = ("adapter", "version", "session_id", "action_id", "cursor", "stage", "duration_ms", "code")
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3


class RedactFilter(logging.Filter):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.data_dir = data_dir

    def filter(self, record: logging.LogRecord) -> bool:
        from robin.redact.scanner import for_data_dir

        rules = for_data_dir(self.data_dir)
        record.msg = rules.redact(record.getMessage())[0]
        record.args = ()
        if record.exc_info:
            record.exc_text = rules.redact(logging.Formatter().formatException(record.exc_info))[0]
            record.exc_info = None
        for name in FIELDS:
            value = getattr(record, name, None)
            if isinstance(value, str):
                setattr(record, name, rules.redact(value)[0])
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in FIELDS:
            if getattr(record, name, None) is not None:
                out[name] = getattr(record, name)
        if record.exc_text:
            out["error"] = record.exc_text.splitlines()[-1]  # the safe last line, not the full trace
        return json.dumps(out, ensure_ascii=False, default=str)


def setup(data_dir: Path, level: int = logging.INFO) -> Path:
    """Send Robin's logs to the data directory's log file. Idempotent."""
    path = secure_mkdir(data_dir / "logs") / "robin.log"
    root = logging.getLogger("robin")
    for h in list(root.handlers):
        if getattr(h, "_robin", None) == str(path):
            return path
        if getattr(h, "_robin", None):  # set up for another data directory: replace it
            root.removeHandler(h)
            h.close()
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS)
    handler._robin = str(path)  # type: ignore[attr-defined]
    handler.addFilter(RedactFilter(data_dir))
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    if path.exists():
        os.chmod(path, 0o600)
    return path
