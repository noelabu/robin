"""The web client in a real browser against the real stack. Runs only with ROBIN_E2E=1 (needs Playwright)."""

import asyncio
import os
import re
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


def sessions_view(page):
    """These tests drive the Sessions view; the dashboard remembers it, the way a returning user has it."""
    page.add_init_script(
        "try { localStorage.setItem('robin.view', 'sessions');"
        " localStorage.setItem('robin.projects', 'open') } catch (e) {}"
    )
    return page


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
        page = sessions_view(browser.new_page())
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
        expect(page.locator(".session-item").first).to_contain_text("Working", timeout=10_000)
        page.locator(".session-item").first.click()
        expect(page.locator(".feed-item").first).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Turn started").first).to_be_visible()

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
        first = sessions_view(browser.new_context().new_page())
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
        page = sessions_view(browser.new_page())
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
        expect(page.get_by_text("Turn started").first).to_be_visible()
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
        page = sessions_view(browser.new_page())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        # counted from the Sessions view; from the Map a session is its card, then "Open in Sessions"
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
        page = sessions_view(browser.new_page())
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


def test_keyboard_only_reaches_every_session_and_keeps_focus(running):
    from playwright.sync_api import expect, sync_playwright

    from robin.events.ingest import ensure_session

    store, port, auth = running
    for i in range(60):
        ensure_session(store, runtime="claude", mode="observed", execution_cwd=f"/w/app{i}")
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = sessions_view(browser.new_page())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.locator(".session-item").first).to_contain_text("app59", timeout=10_000)

        # the list is one Tab stop, and the keyboard reaches its far end
        assert page.locator(".session-item[tabindex='0']").count() == 1
        page.locator(".session-item[tabindex='0']").focus()
        page.keyboard.press("End")
        expect(page.locator(":focus")).to_contain_text("app0")
        page.keyboard.press("Enter")
        expect(page.locator(".detail")).to_contain_text("/w/app0")

        # closing a panel returns focus to the control that opened it
        page.get_by_role("button", name="Launch session").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#launch-title")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("button", name="Launch session")).to_be_focused()
        page.get_by_role("button", name="Settings").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#settings-title")).to_be_focused()

        try:
            page.add_script_tag(url="https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js")
        except Exception:
            browser.close()
            return  # offline: the axe pass needs the network
        violations = page.evaluate(
            "axe.run(document, {runOnly: ['wcag2a', 'wcag2aa', 'wcag21aa']})"
            ".then(r => r.violations.map(v => v.id))"
        )
        assert violations == []
        browser.close()


def test_load_older_pages_in_every_observed_session(running, monkeypatch):
    from playwright.sync_api import expect, sync_playwright

    from robin.events.ingest import ensure_session
    from robin.service import app as service_app

    store, port, auth = running
    monkeypatch.setattr(service_app, "SNAPSHOT_SESSIONS", 20)
    for i in range(450):
        ensure_session(store, runtime="claude", mode="observed", execution_cwd=f"/w/app{i}")
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = sessions_view(browser.new_page())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_label("Sessions, 20")).to_be_visible(timeout=10_000)
        more = page.get_by_role("button", name="load older")
        expect(more).to_contain_text("20 of 450")
        more.click()
        expect(page.get_by_label("Sessions, 220")).to_be_visible()
        more.click()
        expect(page.get_by_label("Sessions, 420")).to_be_visible()
        more.click()
        expect(page.get_by_label("Sessions, 450")).to_be_visible()
        expect(more).to_have_count(0)  # nothing older left
        browser.close()


def test_refreshing_or_closing_the_browser_leaves_running_work_alone(running):
    from playwright.sync_api import expect, sync_playwright

    store, port, auth = running
    folder = store.data_dir.parent / "close-project"
    folder.mkdir()
    cli = httpx.Client(
        base_url=f"http://127.0.0.1:{port}/api/v1", headers={"authorization": f"Bearer {auth.cli_token}"}
    )
    sid = cli.post(
        "/sessions/launch",
        json={"runtime": "fake", "cwd": str(folder), "prompt": "go", "action_id": f"e2e-{uuid.uuid4().hex}"},
    ).json()["session_id"]

    def spawned():
        return [
            e
            for e in cli.get(f"/events?session_id={sid}&limit=500").json()["events"]
            if e["event_type"] == "connection.connected"
        ]

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = sessions_view(browser.new_page())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.locator(".session-item").first).to_be_visible(timeout=10_000)
        page.locator(".session-item").first.click()
        expect(page.locator(".feed-item").first).to_be_visible(timeout=10_000)
        page.reload()
        expect(page.get_by_text("Supervisor connected")).to_be_visible(timeout=10_000)
        expect(page.locator(".session-item").first).to_be_visible(timeout=10_000)
        browser.close()

    time.sleep(1)
    s = cli.get(f"/sessions/{sid}").json()
    assert s["connection_state"] == "connected"
    assert s["runtime_state"] in ("running", "waiting_for_input")
    assert len(spawned()) == 1  # never relaunched

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = sessions_view(browser.new_page())
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.locator(".session-item").first).to_contain_text(
            re.compile("Working|Needs input"), timeout=10_000
        )
        browser.close()
    assert len(spawned()) == 1
    cli.post(f"/sessions/{sid}/actions", json={"type": "stop", "action_id": f"e2e-{uuid.uuid4().hex}"})


def test_the_map_shows_sessions_and_opens_one_in_sessions(running):
    from playwright.sync_api import expect, sync_playwright

    from test_context import observed_session_with

    store, port, auth = running
    sid = observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": "Draw the map"}),
            (
                "tool.started",
                {"type": "tool_started", "tool": "Edit", "input": {"file_path": "/w/app/App.tsx"}},
            ),
        ],
    )
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/#bootstrap={auth.new_bootstrap()}")
        expect(page.get_by_role("tab", name="Map")).to_have_attribute("aria-selected", "true", timeout=10_000)
        card = page.locator(f"[data-card='{sid}']")
        expect(card).to_contain_text("Draw the map", timeout=10_000)
        expect(page.get_by_role("button", name="Now: claude Edit")).to_be_visible()
        try:
            page.add_script_tag(url="https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js")
            axe = True
        except Exception:
            axe = False  # offline: the axe pass needs the network
        if axe:
            violations = page.evaluate(
                "axe.run(document, {runOnly: ['wcag2a', 'wcag2aa', 'wcag21aa']})"
                ".then(r => r.violations.map(v => v.id + ': ' + v.nodes.map(n => n.target).join(' ')))"
            )
            assert violations == []
        card.click()  # 1: its tasks (here, its changed files) open below it
        panel = page.get_by_role("region", name="Tasks: Draw the map")
        expect(panel).to_contain_text("App.tsx")
        expect(card).to_contain_text("Hide tasks")
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
        expect(card).to_be_focused()
        card.click()
        panel.get_by_role("button", name="Open in Sessions").click()  # 2
        expect(page.get_by_role("tab", name="Sessions")).to_have_attribute("aria-selected", "true")
        expect(page.locator(".detail")).to_be_visible()
        browser.close()


def test_follows_the_system_appearance_unless_a_theme_is_chosen(running):
    from playwright.sync_api import expect, sync_playwright

    _, port, _ = running
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_context(color_scheme="light").new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        expect(page.locator("body")).to_have_css("background-color", "rgb(246, 243, 252)")

        chosen = browser.new_context(color_scheme="light").new_page()
        chosen.add_init_script("try { localStorage.setItem('robin.theme', 'dark') } catch (e) {}")
        chosen.goto(f"http://127.0.0.1:{port}/")
        expect(chosen.locator("html")).to_have_attribute("data-theme", "dark")
        expect(chosen.locator("body")).to_have_css("background-color", "rgb(7, 5, 12)")
        browser.close()
