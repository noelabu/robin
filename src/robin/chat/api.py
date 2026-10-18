"""The service calls the chat makes, over the same loopback endpoint and token as the CLI."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from robin.chat.model import parse_sse


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class ChatApi:
    def __init__(self, url: str, token: str, session_id: str):
        self.session_id = session_id
        self.http = httpx.AsyncClient(base_url=f"{url}/api/v1", headers={"authorization": f"Bearer {token}"})

    @classmethod
    def for_data_dir(cls, data_dir: Path, session_id: str) -> "ChatApi":
        from robin.cli import connection

        url, token = connection(data_dir)
        return cls(url, token, session_id)

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = await self.http.request(method, path, json=body, timeout=30)
        except httpx.HTTPError as exc:
            raise ChatError("unavailable", "Robin isn't answering") from exc
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            e = data.get("error", {}) if isinstance(data, dict) else {}
            raise ChatError(str(e.get("code", r.status_code)), str(e.get("message", r.text)))
        return data

    async def session(self, session_id: str | None = None) -> dict[str, Any]:
        return dict(await self._call("GET", f"/sessions/{session_id or self.session_id}"))

    async def conversation(self, session_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
        page = await self._call("GET", f"/sessions/{session_id or self.session_id}/conversation")
        return list(page["messages"]), int(page.get("cursor") or 0)

    async def attention(self) -> list[dict[str, Any]]:
        return list((await self._call("GET", f"/attention?session_id={self.session_id}"))["items"])

    async def act(self, kind: str, text: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"type": kind, "action_id": _action_id()}
        if text is not None:
            body["text"] = text
        return dict(await self._call("POST", f"/sessions/{self.session_id}/actions", body))

    async def respond(
        self, attention_id: str, decision: str, answers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision, "action_id": _action_id()}
        if answers is not None:
            body["answers"] = answers
        return dict(await self._call("POST", f"/attention/{attention_id}/responses", body))

    async def stream(self, after: int) -> AsyncIterator[tuple[str, int | None, dict[str, Any]]]:
        """A "connected" frame, then every event after the cursor as it commits. Heartbeats arrive every
        15 s, so a quiet 45 s means the connection is gone."""
        timeout = httpx.Timeout(10, read=45)
        async with self.http.stream("GET", "/stream", params={"after": after}, timeout=timeout) as r:
            if r.status_code >= 400:
                raise ChatError(str(r.status_code), "the stream was refused")
            yield "connected", None, {}  # live from here, even before anything happens
            buffer: list[str] = []
            async for line in r.aiter_lines():
                buffer.append(line)
                if line == "":
                    for frame in parse_sse(buffer):
                        yield frame
                    buffer = []


def _action_id() -> str:
    return f"chat-{uuid.uuid4().hex}"
