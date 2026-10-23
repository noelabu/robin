"""Remote access over Tailscale: who counts as the owner's device, the whois cache, the old serve mapping."""

import asyncio
import json
import subprocess

import pytest

from robin import remote

ME = 1777
STATUS = {
    "BackendState": "Running",
    "Self": {
        "DNSName": "Mac.tail1.ts.net.",
        "TailscaleIPs": ["100.80.0.1", "fd7a:115c:a1e0::1"],
        "UserID": ME,
    },
    "User": {str(ME): {"LoginName": "me@example.com"}},
    "CurrentTailnet": {},
}
PHONE = "100.80.0.2"


def who(user=ME, profile=ME, tags=None, sharer=None):
    return {"Node": {"User": user, "Tags": tags, "Sharer": sharer}, "UserProfile": {"ID": profile}}


class FakeTailscale:
    def __init__(self, status=STATUS, web=None, whois=None):
        self.status, self.web, self.whois = status, web, whois if whois is not None else who()
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        assert argv[0] == "/fake/tailscale" and kw["timeout"] and not kw.get("shell")
        args = argv[1:]
        self.calls.append(args)
        out = {
            ("status", "--json"): self.status,
            ("serve", "status", "--json"): {"Web": self.web} if self.web else {},
        }.get(tuple(args))
        if args[:2] == ["whois", "--json"]:
            out = self.whois
        return subprocess.CompletedProcess(argv, 0, json.dumps(out) if out is not None else "", "")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(remote, "find_cli", lambda: "/fake/tailscale")
    ts = FakeTailscale()
    monkeypatch.setattr(remote.subprocess, "run", ts)
    return ts


def me():
    return remote.Tailnet(
        ips=("100.80.0.1", "fd7a:115c:a1e0::1"), user_id=ME, login="me@example.com", fqdn="mac.tail1.ts.net"
    )


def test_status_reads_this_mac(fake):
    assert remote.tailnet_status() == me()
    assert me().names == ("mac", "mac.tail1.ts.net", "100.80.0.1", "[fd7a:115c:a1e0::1]")


def test_status_without_magicdns_has_only_addresses(fake):
    fake.status = {**STATUS, "CurrentTailnet": {"MagicDNSEnabled": False}}
    assert remote.tailnet_status().names == ("100.80.0.1", "[fd7a:115c:a1e0::1]")


def test_status_logged_out(fake):
    fake.status = {"BackendState": "NeedsLogin"}
    with pytest.raises(remote.RemoteError) as e:
        remote.tailnet_status()
    assert e.value.code == "tailscale_logged_out"


def test_missing_cli(monkeypatch):
    monkeypatch.undo()  # the autouse guard replaced find_cli; test the real one with nothing installed
    monkeypatch.setattr(remote.shutil, "which", lambda _: None)
    monkeypatch.setattr(remote, "APP_CLI", remote.Path("/nonexistent/tailscale"))
    with pytest.raises(remote.RemoteError) as e:
        remote.find_cli()
    assert e.value.code == "tailscale_missing"


@pytest.mark.parametrize(
    ("ip", "answer", "ok"),
    [
        (PHONE, who(), True),
        ("fd7a:115c:a1e0::2", who(), True),
        (PHONE, who(user=99, profile=99), False),  # another user's device
        (PHONE, who(profile=99), False),
        (PHONE, who(tags=["tag:server"]), False),
        (PHONE, who(sharer=42), False),  # shared in from another tailnet
        ("100.80.0.1", who(), False),  # this Mac: a local process connecting to its own Tailscale address
        ("fd7a:115c:a1e0::1", who(), False),
        ("192.168.1.20", who(), False),  # not a tailnet address
        ("127.0.0.1", who(), False),
        ("not-an-ip", who(), False),
        (PHONE, {}, False),
    ],
)
def test_only_the_owners_other_devices_are_trusted(ip, answer, ok):
    assert remote.trusted(ip, me(), answer) is ok


def test_trust_is_cached_and_reset(fake, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    trust = remote.TailnetTrust()
    assert asyncio.run(trust.check(PHONE)) is False  # nothing known about this Mac yet
    trust.reset(me())
    assert asyncio.run(trust.check(PHONE)) is True
    fake.whois = who(user=99, profile=99)
    assert asyncio.run(trust.check(PHONE)) is True  # cached
    clock[0] += remote.TRUSTED_TTL + 1
    assert asyncio.run(trust.check(PHONE)) is False  # removed from the tailnet: refused after the TTL
    assert len([c for c in fake.calls if c[0] == "whois"]) == 2
    trust.reset()
    assert asyncio.run(trust.check(PHONE)) is False


def test_whois_failure_is_refused(fake, monkeypatch):
    def broken(*a):
        raise remote.RemoteError(502, "tailscale_failed", "boom")

    monkeypatch.setattr(remote, "whois", broken)
    trust = remote.TailnetTrust()
    trust.reset(me())
    assert asyncio.run(trust.check(PHONE)) is False


def mapped(proxy):
    return {"mac.tail1.ts.net:443": {"Handlers": {"/": {"Proxy": proxy}}}}


def test_legacy_serve_is_removed_only_when_it_is_robins(fake):
    assert remote.remove_legacy_serve(7333) is None
    fake.web = mapped("http://127.0.0.1:3000")
    assert remote.remove_legacy_serve(7333) is None
    assert ["serve", "--yes", "--https=443", "off"] not in fake.calls
    fake.web = mapped("http://127.0.0.1:7333")
    assert remote.remove_legacy_serve(7333) == "https://mac.tail1.ts.net"
    assert fake.calls[-1] == ["serve", "--yes", "--https=443", "off"]


def test_a_hung_tailscale_times_out_with_what_it_said(fake, monkeypatch):
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(
            argv, kw["timeout"], output="To enable, visit: https://login.tailscale.com/x"
        )

    monkeypatch.setattr(remote.subprocess, "run", hang)
    with pytest.raises(remote.RemoteError) as e:
        remote.tailnet_status()
    assert "login.tailscale.com" in e.value.message


def test_never_binds_a_wildcard_address():
    with pytest.raises(AssertionError):
        remote._bind("0.0.0.0", 0)
    with pytest.raises(AssertionError):
        remote._bind("::", 0)


def test_setting_round_trips(store):
    assert remote.settings(store) == {"enabled": False}
    assert remote.set_settings(store, True) == {"enabled": True}
    assert remote.set_settings(store, False) == {"enabled": False}


def test_a_refused_device_is_asked_again_sooner_than_a_trusted_one(fake, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    trust = remote.TailnetTrust()
    trust.reset(me())
    fake.whois = who(user=99, profile=99)
    assert asyncio.run(trust.check(PHONE)) is False
    fake.whois = who()  # the phone is moved to the owner's account
    clock[0] += remote.REFUSED_TTL + 1
    assert remote.REFUSED_TTL + 1 < remote.TRUSTED_TTL
    assert asyncio.run(trust.check(PHONE)) is True


async def test_listener_lets_go_when_tailscale_logs_out(monkeypatch):
    from robin.service.main import free_port

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    here = remote.Tailnet(ips=("127.0.0.1",), user_id=ME, login="me@example.com", fqdn=None)
    up = [True]

    def status():
        if not up[0]:
            raise remote.RemoteError(409, "tailscale_logged_out", "Tailscale isn't connected")
        return here

    monkeypatch.setattr(remote, "tailnet_status", status)
    monkeypatch.setattr(remote, "POLL_S", 0.01)
    changes = []
    port = free_port()
    trust = remote.TailnetTrust()
    listener = remote.TailnetListener(app, port, trust, lambda m, b: changes.append((m, b)))
    listener.start()
    try:
        await asyncio.wait_for(listener.ready.wait(), 5)
        assert listener.bound == {"127.0.0.1"} and trust.me == here
        assert changes[-1] == (here, frozenset({"127.0.0.1"}))
        _, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()
        up[0] = False
        for _ in range(500):
            if changes[-1][0] is None:
                break
            await asyncio.sleep(0.01)
        assert changes[-1] == (None, frozenset())
        assert listener.bound == frozenset() and trust.me is None
        assert listener.problem == "Tailscale isn't connected"
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        await listener.stop()
