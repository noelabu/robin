"""Framing shared by the supervisor, runtime hosts and service clients.

Every message is one JSON object per line. Supervisor <-> service messages
carry the protocol version in `hello`; runtime host messages are events
(host -> supervisor, stdout) and commands (supervisor -> host, stdin).
"""

import json

PROTOCOL_VERSION = 1
MAX_LINE = 1024 * 1024  # 1 MiB, matches the TR-082 per-record parser limit


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":"), default=str) + "\n").encode()


def decode(line: bytes) -> dict:
    return json.loads(line)
