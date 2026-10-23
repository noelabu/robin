"""Remote access over Tailscale: open the dashboard from the user's other devices on their tailnet.

Opt-in and off by default. Turned on, the service also listens on this Mac's Tailscale addresses (never a
wildcard address) and signs in, without a link, any device that `tailscale whois` says belongs to the same
Tailscale user as this Mac. Other users' devices, shared-in and tagged nodes, and this Mac's own addresses
are refused. Identity comes from the TCP peer address, never from a header. Loopback rules are unchanged.
"""

import asyncio
import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.store import Store
from robin.store.models import Meta

SETTING = "remote"
APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/tailscale")
TIMEOUT_S = 8.0
TRUSTED_TTL = 60.0  # a device removed from the tailnet keeps access at most this long
REFUSED_TTL = 15.0
CACHE_MAX = 256
POLL_S = 30.0  # how soon a logout, an address change or a wake from sleep is noticed
BACKOFF_MAX = 60.0
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
log = logging.getLogger(__name__)


class RemoteError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def settings(store: Store) -> dict[str, Any]:
    with store.reader.connect() as conn:
        raw = conn.execute(select(Meta.value).where(Meta.key == SETTING)).scalar()
    value = json.loads(raw) if raw else {}
    return {"enabled": bool(value.get("enabled"))}


def set_settings(store: Store, enabled: bool) -> dict[str, Any]:
    value = json.dumps({"enabled": enabled})

    def work(conn: Any) -> None:
        conn.execute(
            sqlite_insert(Meta)
            .values(key=SETTING, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )

    store.writer.call(work)
    return settings(store)


# -- the tailscale CLI ---------------------------------------------------------


def find_cli() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    if APP_CLI.exists():
        return str(APP_CLI)
    raise RemoteError(
        503, "tailscale_missing", "Tailscale isn't installed (https://tailscale.com/download/mac)"
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [find_cli(), *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or exc.stderr or b""
        said = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw).strip()
        message = said or f"`tailscale {' '.join(args)}` didn't finish in {TIMEOUT_S:.0f} seconds"
        raise RemoteError(502, "tailscale_failed", message) from exc


def _json(*args: str) -> dict[str, Any]:
    r = _run(*args)
    if r.returncode != 0:
        raise RemoteError(502, "tailscale_failed", (r.stderr or r.stdout).strip() or "tailscale failed")
    try:
        value = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RemoteError(502, "tailscale_failed", "tailscale returned output Robin can't read") from exc
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Tailnet:
    """This Mac on its tailnet."""

    ips: tuple[str, ...]
    user_id: int
    login: str
    fqdn: str | None  # MagicDNS name, e.g. mac.tail1234.ts.net; None with MagicDNS off

    @property
    def names(self) -> tuple[str, ...]:
        """Every way a browser may address this Mac: short name, full name, addresses."""
        names = [self.fqdn.split(".")[0], self.fqdn] if self.fqdn else []
        names += [f"[{ip}]" if ":" in ip else ip for ip in self.ips]
        return tuple(names)


def tailnet_status() -> Tailnet:
    status = _json("status", "--json")
    if status.get("BackendState") != "Running":
        raise RemoteError(409, "tailscale_logged_out", "Tailscale isn't connected; open Tailscale and log in")
    me = status.get("Self") or {}
    ips = tuple(str(ip) for ip in me.get("TailscaleIPs") or [])
    user_id = me.get("UserID")
    if not ips or not isinstance(user_id, int):
        raise RemoteError(502, "tailscale_failed", "Tailscale didn't say which addresses this Mac has")
    login = str(((status.get("User") or {}).get(str(user_id)) or {}).get("LoginName") or user_id)
    magic = (status.get("CurrentTailnet") or {}).get("MagicDNSEnabled") is not False
    fqdn = str(me.get("DNSName") or "").rstrip(".").lower() or None
    return Tailnet(ips=ips, user_id=user_id, login=login, fqdn=fqdn if magic else None)


def whois(ip: str) -> dict[str, Any]:
    return _json("whois", "--json", ip)


def trusted(ip: str, me: Tailnet, who: dict[str, Any]) -> bool:
    """A device of this Mac's owner, reached over the tailnet, and not this Mac itself."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if ip in me.ips or not any(addr in net for net in TAILNET_NETS):
        return False
    node, profile = who.get("Node") or {}, who.get("UserProfile") or {}
    if node.get("Tags") or node.get("Sharer"):
        return False
    return node.get("User") == me.user_id and profile.get("ID") == me.user_id


class TailnetTrust:
    """Per-peer answers from `tailscale whois`, cached briefly."""

    def __init__(self) -> None:
        self.me: Tailnet | None = None
        self._cache: dict[str, tuple[bool, float]] = {}

    def reset(self, me: Tailnet | None = None) -> None:
        self.me = me
        self._cache.clear()

    async def check(self, ip: str) -> bool:
        me = self.me
        if me is None:
            return False
        now = time.monotonic()
        hit = self._cache.get(ip)
        if hit is not None and now < hit[1]:
            return hit[0]
        try:
            ok = trusted(ip, me, await asyncio.to_thread(whois, ip))
        except RemoteError:
            ok = False
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[ip] = (ok, now + (TRUSTED_TTL if ok else REFUSED_TTL))
        return ok


def _bind(ip: str, port: int) -> socket.socket:
    addr = ipaddress.ip_address(ip)
    assert not addr.is_unspecified, "never a wildcard address"
    sock = socket.socket(socket.AF_INET6 if addr.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if addr.version == 6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((ip, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class TailnetListener:
    """Serves the app on this Mac's Tailscale addresses while remote access is on.

    A second uvicorn server in the service's loop, started with sockets Robin binds itself, so it can follow
    Tailscale coming up late, logging out, or changing address. `on_change(me, bound ips)` keeps the auth
    rules in step with what is actually listening.
    """

    def __init__(
        self,
        app: Any,
        port: int,
        trust: TailnetTrust,
        on_change: Callable[[Tailnet | None, frozenset[str]], None],
    ):
        self.app, self.port, self.trust, self.on_change = app, port, trust, on_change
        self.problem: str | None = None
        self.bound: frozenset[str] = frozenset()
        self._server: uvicorn.Server | None = None
        self._sockets: list[socket.socket] = []
        self._task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()  # set after each attempt, bound or not

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.ready.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close()
        self.trust.reset()
        self.on_change(None, frozenset())
        self.problem = None

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            try:
                me = await asyncio.to_thread(tailnet_status)
                if frozenset(me.ips) != self.bound:
                    await self._open(me.ips)
                if self.trust.me != me:
                    self.trust.reset(me)
                self.on_change(me, self.bound)
                self.problem, backoff, wait = None, 2.0, POLL_S
            except Exception as exc:  # keep retrying whatever went wrong; only stop() ends this loop
                self.problem = (
                    exc.message if isinstance(exc, RemoteError) else f"can't listen on Tailscale: {exc}"
                )
                log.warning("remote access waiting: %s", self.problem)
                await self._close()
                self.trust.reset()
                self.on_change(None, frozenset())
                wait, backoff = backoff, min(backoff * 2, BACKOFF_MAX)
            self.ready.set()
            await asyncio.sleep(wait)

    async def _open(self, ips: tuple[str, ...]) -> None:
        await self._close()
        sockets = []
        try:
            for ip in ips:
                sockets.append(_bind(ip, self.port))
        except OSError:
            for s in sockets:
                s.close()
            raise
        config = uvicorn.Config(
            self.app,
            lifespan="off",
            log_level="warning",
            proxy_headers=False,
            server_header=False,
            date_header=False,
            timeout_graceful_shutdown=2,
        )
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        try:
            await server.startup(sockets=sockets)
        except BaseException:
            for s in sockets:
                s.close()
            raise
        self._server, self._sockets, self.bound = server, sockets, frozenset(ips)

    async def _close(self) -> None:
        server, sockets = self._server, self._sockets
        self._server, self._sockets, self.bound = None, [], frozenset()
        if server is not None:
            server.force_exit = True  # the app's lifespan belongs to the loopback server
            await server.shutdown(sockets=sockets)


# -- the earlier `tailscale serve` setup -----------------------------------------


def remove_legacy_serve(port: int) -> str | None:
    """Remove the https mapping an earlier Robin made, only if it still points at this service."""
    me = tailnet_status()
    if me.fqdn is None:
        return None
    web = _json("serve", "status", "--json").get("Web") or {}
    proxy = (((web.get(f"{me.fqdn}:443") or {}).get("Handlers") or {}).get("/") or {}).get("Proxy")
    if not proxy or str(proxy).rstrip("/") != f"http://127.0.0.1:{port}":
        return None
    r = _run("serve", "--yes", "--https=443", "off")
    if r.returncode != 0:
        raise RemoteError(502, "tailscale_failed", (r.stderr or r.stdout).strip() or "tailscale serve failed")
    return f"https://{me.fqdn}"
