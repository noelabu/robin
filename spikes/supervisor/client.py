"""Phase 0 spike: a stand-in for the Robin service's supervisor client.

Connects with a durable cursor, records every event it receives, persists
the cursor after each event and acknowledges it.

    python client.py --data DIR watch [--seconds N]
    python client.py --data DIR launch fake|claude [--prompt TEXT] [--cwd DIR]
    python client.py --data DIR send SESSION TEXT
    python client.py --data DIR interrupt SESSION
    python client.py --data DIR approve|deny SESSION REQUEST_ID
    python client.py --data DIR status | shutdown
"""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from protocol import MAX_LINE, PROTOCOL_VERSION, decode, encode


class Client:
    def __init__(self, data: Path):
        self.data = data
        self.cursor_path = data / "client.cursor"
        self.log_path = data / "client-events.jsonl"
        self.cursor = int(self.cursor_path.read_text()) if self.cursor_path.exists() else 0
        self.replies: dict[str, asyncio.Future] = {}

    async def connect(self):
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.data / "supervisor.sock"), limit=MAX_LINE)
        self.writer.write(encode({"type": "hello", "protocol": PROTOCOL_VERSION, "after": self.cursor}))
        await self.writer.drain()
        hello = decode(await self.reader.readline())
        assert hello["type"] == "hello_ok", hello
        self.hello = hello
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        with open(self.log_path, "a") as log:
            while line := await self.reader.readline():
                msg = decode(line)
                if msg["type"] == "event":
                    if msg["seq"] <= self.cursor:
                        continue  # replay overlap; already processed
                    log.write(json.dumps(msg) + "\n")
                    log.flush()
                    self.cursor = msg["seq"]
                    self.cursor_path.write_text(str(self.cursor))
                    self.writer.write(encode({"type": "ack", "seq": self.cursor}))
                    print(json.dumps({k: msg[k] for k in ("seq", "session", "event")}), flush=True)
                elif msg["type"] == "reply" and msg.get("corr") in self.replies:
                    self.replies.pop(msg["corr"]).set_result(msg)

    async def request(self, msg: dict) -> dict:
        corr = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.replies[corr] = fut
        self.writer.write(encode({**msg, "corr": corr}))
        await self.writer.drain()
        return await asyncio.wait_for(fut, 10)


async def main(args):
    c = Client(args.data.resolve())
    await c.connect()
    if args.action == "watch":
        await asyncio.sleep(args.seconds)
        return
    if args.action == "launch":
        msg = {"type": "launch", "runtime": args.rest[0], "prompt": args.prompt, "cwd": args.cwd}
    elif args.action == "send":
        msg = {"type": "command", "session": args.rest[0], "command": {"cmd": "send", "text": args.rest[1]}}
    elif args.action == "interrupt":
        msg = {"type": "command", "session": args.rest[0], "command": {"cmd": "interrupt"}}
    elif args.action in ("approve", "deny"):
        msg = {"type": "command", "session": args.rest[0],
               "command": {"cmd": "approval", "request_id": args.rest[1], "allow": args.action == "approve"}}
    elif args.action == "stop":
        msg = {"type": "command", "session": args.rest[0], "command": {"cmd": "stop"}}
    else:
        msg = {"type": args.action}
    print(json.dumps(await c.request(msg)), flush=True)
    await asyncio.sleep(args.seconds)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=0.5)
    ap.add_argument("--prompt")
    ap.add_argument("--cwd", default=str(Path.cwd()))
    ap.add_argument("action", choices=["watch", "launch", "send", "interrupt", "approve", "deny",
                                       "stop", "status", "shutdown"])
    ap.add_argument("rest", nargs="*")
    asyncio.run(main(ap.parse_args()))
