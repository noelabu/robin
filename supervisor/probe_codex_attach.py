"""Phase 0 probe: can a second client attach to a live Codex thread? (FR-037 attach mode)

    uv run --with websockets python supervisor/probe_codex_attach.py

Starts a private `codex app-server --listen ws://127.0.0.1:<port>` (the user's
own Codex daemon is not touched) and connects two WebSocket clients, one
JSON-RPC message per text frame. Client A starts a thread and runs one turn
(ephemeral threads have no rollout and can't be resumed). Client B sees it in
thread/loaded/list and subscribes with thread/resume. A runs a second turn
while B records which live events it receives; then B sends input into the
same thread to check whether control is shared too.
"""

import asyncio
import json
import socket
import tempfile

import websockets


class Client:
    def __init__(self, name, url):
        self.name, self.url, self.next_id, self.waiting, self.notes = name, url, 0, {}, []

    async def start(self):
        self.ws = await websockets.connect(self.url, max_size=1 << 22)
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {"clientInfo": {"name": f"robin-probe-{self.name}", "version": "0"}})
        await self.ws.send(json.dumps({"method": "initialized"}))

    async def call(self, method, params):
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiting[self.next_id] = fut
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, 60)

    async def _read(self):
        try:
            async for frame in self.ws:
                await self._handle(json.loads(frame))
        except websockets.ConnectionClosed:
            pass  # the server doesn't send a close frame on teardown

    async def _handle(self, msg):
        if "id" in msg and "method" not in msg:
            fut = self.waiting.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif "method" in msg and "id" not in msg:
            self.notes.append(msg)

    def seen(self, thread, after=0):
        kinds = {}
        for n in self.notes[after:]:
            if n.get("params", {}).get("threadId") == thread:
                kinds[n["method"]] = kinds.get(n["method"], 0) + 1
        return kinds


async def wait_turn(client, after, timeout=120):
    for _ in range(timeout):
        await asyncio.sleep(1)
        if any(n["method"] == "turn/completed" for n in client.notes[after:]):
            return
    raise TimeoutError("turn did not complete")


async def main():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    work = tempfile.mkdtemp(prefix="robin-attach-probe-")
    server = await asyncio.create_subprocess_exec(
        "codex", "app-server", "--listen", f"ws://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    url = f"ws://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                a = Client("A", url)
                await a.start()
                break
            except OSError:
                await asyncio.sleep(0.2)
        started = await a.call("thread/start", {"cwd": work})
        thread = started["result"]["thread"]["id"]
        print(f"A started thread {thread}")
        mark = len(a.notes)
        await a.call("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": "Reply with exactly the word READY."}]})
        await wait_turn(a, mark)

        b = Client("B", url)
        await b.start()
        loaded = await b.call("thread/loaded/list", {})
        print(f"B sees the thread in thread/loaded/list: {thread in json.dumps(loaded.get('result'))}")
        resumed = await b.call("thread/resume", {"threadId": thread})
        print(f"B thread/resume: {'ok' if 'result' in resumed else resumed.get('error')}")

        mark_a, mark_b = len(a.notes), len(b.notes)
        await a.call("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": "Count from 1 to 30, one number per line. Do not use tools."}]})
        await wait_turn(a, mark_a)
        await asyncio.sleep(1)
        live = {"turn/started", "item/agentMessage/delta", "turn/completed"}
        got = set(b.seen(thread, mark_b))
        print(f"1) B observes A's turn live: {'YES' if live <= got else 'NO'} ({sorted(live & got)})")

        mark_a, mark_b = len(a.notes), len(b.notes)
        r = await b.call("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": "Reply with exactly the word HERON."}]})
        if "error" in r:
            print(f"2) B can send input to the thread: NO ({r['error']})")
        else:
            await wait_turn(b, mark_b)
            await asyncio.sleep(1)
            a_got = set(a.seen(thread, mark_a))
            print(f"2) B can send input to the thread: YES; A observes B's turn: "
                  f"{'YES' if {'turn/started', 'turn/completed'} <= a_got else 'NO'}")
        await b.call("thread/unsubscribe", {"threadId": thread})
        await a.ws.close()
        await b.ws.close()
    finally:
        server.terminate()
        await server.wait()


if __name__ == "__main__":
    asyncio.run(main())
