"""The supervisor's wire protocol (TR-006): one JSON object per line over a user-only Unix socket.

Service -> supervisor: hello, ack, launch, command, status, shutdown.
Supervisor -> service: hello_ok / hello_rejected, event, reply.
Runtime host <-> supervisor: events on the host's stdout, commands on its stdin, same framing.

seq is global and monotonic across supervisor generations, so a service cursor is a single integer.
"""

import json
from typing import Any

PROTOCOL_VERSION = 1
# A record carries one runtime event; the corpus has 3.9 MiB records and the parser allows 16 MiB.
MAX_LINE = 17 << 20


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("protocol messages are JSON objects")
    return obj
