"""The web client in a real browser against the real stack. Runs only with ROBIN_E2E=1 (needs Playwright)."""

import asyncio
import os
import threading
import time
import uuid

import httpx
import pytest

from robin.service.main import build, free_port

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_E2E") != "1", reason="set ROBIN_E2E=1 to run")


def launch_browser(p):
    """System Chrome when present (no browser download); otherwise Playwright's own Chromium."""
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch()


@pytest.fixture
def running(supervised):
    store = supervised
    port = free_port()
    server, auth = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield store, port, auth
    server.should_exit = True
    thread.join(10)


def test_sign_in_watch_and_control_a_session(running):
    from playwright.sync_api import expect, sync_playwright

    store, port, auth = running
    project = store.data_dir.parent / "project"
    project.mkdir()
    cli = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"authorization": f"Bearer {auth.cli_token}"}
    )
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        assert "bootstrap" not in page.url  # the secret leaves the address bar

        r = cli.post(
            "/api/v1/sessions/launch",
            json={
                "runtime": "fake",
                "cwd": str(project),
                "prompt": "go",
                "action_id": f"e2e-{uuid.uuid4().hex}",
            },
        )
        assert r.status_code == 202
        expect(page.locator(".session-item").first).to_contain_text("Running", timeout=10_000)
        page.locator(".session-item").first.click()
        expect(page.locator(".feed-item").first).to_be_visible(timeout=10_000)
        expect(page.get_by_text("session.turn_started")).to_be_visible()

        page.get_by_role("button", name="Interrupt").click()
        expect(page.get_by_role("status").filter(has_text="Interrupt:")).to_be_visible()
        expect(page.locator(".detail h2")).to_contain_text("Idle", timeout=10_000)

        page.get_by_role("button", name="Stop session").click()
        expect(page.locator(".detail h2")).to_contain_text("disconnected", timeout=10_000)
        expect(page.get_by_role("button", name="Interrupt")).to_be_disabled()

        # keyboard only: the controls are reachable by Tab
        page.locator("body").focus()
        reached = set()
        for _ in range(25):
            page.keyboard.press("Tab")
            reached.add(page.evaluate("document.activeElement.textContent || document.activeElement.id"))
        assert any("Launch session" in r for r in reached)
        browser.close()


def test_reused_sign_in_link_is_refused(running):
    from playwright.sync_api import expect, sync_playwright

    _, port, auth = running
    link = f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}"
    with sync_playwright() as p:
        browser = launch_browser(p)
        first = browser.new_context().new_page()
        first.goto(link)
        expect(first.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        second = browser.new_context().new_page()  # someone else replays the same link
        second.goto(link)
        expect(second.get_by_text("already used or has expired")).to_be_visible(timeout=10_000)
        browser.close()
