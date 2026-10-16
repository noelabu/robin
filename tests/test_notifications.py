"""Notifications (FR-033): opt-in, once per event, quiet where the user is looking, no message text."""

import time

from robin import notifications
from test_findings import aid, launch, say, wait


def collect(monkeypatch):
    sent: list[notifications.Note] = []
    monkeypatch.setattr(notifications, "send", sent.append)
    return sent


def test_off_by_default_and_nothing_from_before_turning_on(api, monkeypatch):
    store, c, task_id, _ = api
    sent = collect(monkeypatch)
    assert c.get("/settings/notifications").json() == {
        "enabled": False,
        "kinds": ["input", "failed", "finished"],
    }
    sid = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "prompt": "go", "action_id": aid()}
    ).json()["session_id"]
    wait(lambda: c.get(f"/attention?session_id={sid}").json()["items"])  # it asked, with notifications off
    time.sleep(0.3)
    assert sent == []

    assert c.put("/settings/notifications", json={"enabled": True}).json()["enabled"]
    time.sleep(0.5)
    assert sent == []  # the request from before isn't announced now


def test_input_and_finished_notify_once_without_message_text(api, monkeypatch):
    store, c, task_id, _ = api
    sent = collect(monkeypatch)
    c.put("/settings/notifications", json={"enabled": True})
    sid = c.post(
        "/sessions/launch",
        json={"runtime": "fake", "task_id": task_id, "prompt": "secret plan go", "action_id": aid()},
    ).json()["session_id"]
    wait(lambda: [n for n in sent if n.kind == "input"])
    (note,) = [n for n in sent if n.kind == "input"]
    assert note.session_id == sid and "needs your input" in note.body and "Review auth" in note.body
    assert "secret" not in note.body

    finisher = launch(c, task_id)
    say(c, finisher, "tool-finding:{}")  # the fake ends this turn at once
    wait(lambda: [n for n in sent if n.kind == "finished"])
    time.sleep(0.5)
    assert len([n for n in sent if n.kind == "input"]) == 1  # later wake-ups don't repeat it
    assert len([n for n in sent if n.kind == "finished"]) == 1


def test_interrupted_turns_and_unwanted_kinds_stay_quiet(api, monkeypatch):
    _, c, task_id, _ = api
    sent = collect(monkeypatch)
    c.put("/settings/notifications", json={"enabled": True, "kinds": ["failed", "finished"]})
    sid = c.post(
        "/sessions/launch", json={"runtime": "fake", "task_id": task_id, "prompt": "go", "action_id": aid()}
    ).json()["session_id"]
    wait(lambda: c.get(f"/attention?session_id={sid}").json()["items"])
    c.post(f"/sessions/{sid}/actions", json={"type": "interrupt", "action_id": aid()})
    wait(lambda: c.get(f"/sessions/{sid}").json()["runtime_state"] == "idle")
    time.sleep(0.5)
    assert sent == []  # input wasn't wanted, and the user interrupted the turn themselves


def test_the_session_on_screen_stays_quiet(api, monkeypatch):
    _, c, task_id, _ = api
    sent = collect(monkeypatch)
    c.put("/settings/notifications", json={"enabled": True})
    watched, other = launch(c, task_id), launch(c, task_id)
    c.put("/presence", json={"session_id": watched})
    say(c, watched, "tool-finding:{}")
    say(c, other, "tool-finding:{}")
    wait(lambda: [n for n in sent if n.session_id == other])
    time.sleep(0.3)
    assert not [n for n in sent if n.session_id == watched]

    c.put("/presence", json={"session_id": None})  # the dashboard went away
    say(c, watched, "tool-finding:{}")
    wait(lambda: [n for n in sent if n.session_id == watched])


def test_a_burst_is_summarised():
    notes = [notifications.Note(str(i), "finished", "Robin", f"n{i}") for i in range(7)]
    out = notifications.condense(notes)
    assert [n.body for n in out[:2]] == ["n0", "n1"] and out[2].body.startswith("5 more")


def test_text_reaches_osascript_only_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications.subprocess, "run", lambda argv, **kw: calls.append(argv))
    notifications.send(notifications.Note("s", "input", "Robin", '" & do shell script "touch /tmp/x'))
    (argv,) = calls
    script = " ".join(argv[: argv.index("end run") + 1])
    assert "touch" not in script and argv[-1] == '" & do shell script "touch /tmp/x'
