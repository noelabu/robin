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


def test_approve_from_the_attention_panel(running):
    from playwright.sync_api import expect, sync_playwright

    store, port, auth = running
    project = store.data_dir.parent / "approve-project"
    project.mkdir()
    cli = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"authorization": f"Bearer {auth.cli_token}"}
    )
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        cli.post(
            "/api/v1/sessions/launch",
            json={
                "runtime": "fake",
                "cwd": str(project),
                "prompt": "go",
                "action_id": f"e2e-{uuid.uuid4().hex}",
            },
        )
        item = page.locator(".attention-item").first
        expect(item).to_contain_text("Bash needs approval", timeout=10_000)
        expect(item).to_contain_text("make test")
        item.get_by_role("button", name="Approve").click()
        expect(page.get_by_text("Nothing is waiting on you.")).to_be_visible(timeout=10_000)
        expect(page.get_by_text("session.turn_started").first).to_be_visible()
        browser.close()


def test_reach_a_task_session_in_three_clicks_and_see_shared_folder_warnings(running):
    from playwright.sync_api import expect, sync_playwright

    store, port, auth = running
    folder = store.data_dir.parent / "nav-project"
    folder.mkdir()
    cli = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"authorization": f"Bearer {auth.cli_token}"}
    )
    project = cli.post("/api/v1/projects", json={"path": str(folder), "name": "navproj"}).json()
    task = cli.post(
        "/api/v1/tasks",
        json={"project_id": project["project_id"], "title": "Fix login", "objective": "Make it work"},
    ).json()
    sid = cli.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "task_id": task["task_id"], "action_id": f"e2e-{uuid.uuid4().hex}"},
    ).json()["session_id"]
    cli.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": str(folder), "action_id": f"e2e-{uuid.uuid4().hex}"},
    )
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="navproj").click()  # 1
        page.get_by_role("button", name="Fix login").click()  # 2
        expect(page.get_by_role("heading", name="Fix login")).to_be_visible()
        page.locator(".task-sessions").get_by_role("button").first.click()  # 3
        expect(page.locator(".detail .facts")).to_contain_text(str(folder))
        expect(page.get_by_role("alert")).to_contain_text("may be editing this folder", timeout=10_000)
        expect(page.locator(".feed-item").first).to_be_visible(timeout=10_000)  # history for this session
        browser.close()
    for s in cli.get("/api/v1/workspace").json()["sessions"]:
        cli.post(
            f"/api/v1/sessions/{s['session_id']}/actions",
            json={"type": "stop", "action_id": f"e2e-{uuid.uuid4().hex}"},
        )
    assert sid


def test_compare_select_claims_send_and_save_a_synthesis(running):
    from playwright.sync_api import expect, sync_playwright

    store, port, auth = running
    folder = store.data_dir.parent / "compare-project"
    folder.mkdir()
    cli = httpx.Client(
        base_url=f"http://127.0.0.1:{port}/api/v1", headers={"authorization": f"Bearer {auth.cli_token}"}
    )
    pid = cli.post("/projects", json={"path": str(folder), "name": "cmp"}).json()["project_id"]
    task = cli.post("/tasks", json={"project_id": pid, "title": "Review", "objective": "Find bugs"}).json()[
        "task_id"
    ]
    for summary in ("Race in refresh", "Logout leaks token"):
        cli.post(
            "/findings",
            json={
                "task_id": task,
                "summary": summary,
                "claims": [
                    {"text": f"{summary} claim A", "unsupported": True},
                    {"text": f"{summary} claim B", "unsupported": True},
                ],
            },
        )
    cli.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task, "action_id": f"e2e-{uuid.uuid4().hex}"}
    )
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="cmp").click()
        page.get_by_role("button", name="Review").click()
        expect(page.locator(".finding-card")).to_have_count(2, timeout=10_000)
        page.get_by_label("Race in refresh claim A").check()
        page.locator(".finding-card").nth(1).get_by_role("checkbox").first.check()  # all of the second
        page.get_by_role("button", name="Send selected to a session…").click()
        page.get_by_role("button", name="Preview").click()
        share = page.locator(".share")
        expect(share).to_contain_text("claims 1 of 2 selected")
        expect(share).to_contain_text("tokens (approximate)")
        share.get_by_text("Exact text that will be sent").click()
        expect(share.locator(".payload")).to_contain_text("Race in refresh claim A")
        expect(share.locator(".payload")).not_to_contain_text("Race in refresh claim B")
        share.get_by_role("button", name="Confirm and send").click()
        expect(share).to_contain_text("Delivery:", timeout=10_000)
        for _ in range(40):
            share.get_by_role("button", name="Save reply as synthesis").click()
            if "Saved as synthesis" in (share.locator(".status").text_content() or ""):
                break
            time.sleep(0.25)
        expect(share.locator(".status")).to_contain_text("Saved as synthesis")
        browser.close()
