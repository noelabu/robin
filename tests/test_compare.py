"""Compare, share and synthesize (FR-024): claim selection, saved replies, evidence reading, CLI."""

import os

from test_context import preview, publish_user
from test_findings import GOOD, aid, block, launch, say, wait


def two_findings(c, task_id):
    agent = launch(c, task_id)
    say(c, agent, block({**GOOD, "claims": GOOD["claims"] + [{"text": "third", "unsupported": True}]}))
    (f,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    return f, publish_user(c, task_id)


def test_selected_claims_and_a_saved_synthesis(api):
    store, c, task_id, _ = api
    target = launch(c, task_id)
    agent_f, user_f = two_findings(c, task_id)
    p = preview(
        c,
        task_id,
        target,
        findings=[{"id": agent_f["finding_id"], "claims": [1, 3]}, {"id": user_f["finding_id"]}],
    ).json()
    assert "claims 1, 3 of 3 selected" in p["sources"][1]["label"]
    assert (
        "refresh() reads a revoked token" in p["text"]
        and "Probably also affects SSO" in p["text"].split("[source 3")[1]
    )  # claim 2 only appears in the second finding
    bad = preview(c, task_id, target, findings=[{"id": agent_f["finding_id"], "claims": [9]}])
    assert bad.status_code == 422

    assert c.post(f"/context/{p['packet_id']}/save-reply", json={}).status_code == 409  # not delivered yet
    c.post(f"/context/{p['packet_id']}/confirm", json={"payload_hash": p["payload_hash"]})
    first = aid()
    c.post(f"/context/{p['packet_id']}/deliver", json={"action_id": first})
    wait(lambda: c.get(f"/actions/{first}").json()["state"] == "acknowledged")
    saved = wait(
        lambda: (r := c.post(f"/context/{p['packet_id']}/save-reply", json={})).status_code == 201 and r
    )
    s = c.get(f"/artifacts/{saved.json()['artifact_id']}").json()
    assert s["kind"] == "synthesis" and s["content"]["text"].startswith("Synthesis of 3 sources")
    assert s["content"]["author"]["runtime"] == "fake" and "derived" in s["content"]["label"]
    parents = {(i["parent_artifact_id"], i["parent_version"]) for i in s["inputs"]}
    packet_artifact = c.get(f"/context/{p['packet_id']}").json()["artifact_id"]
    assert parents == {(packet_artifact, 1), (agent_f["finding_id"], 1), (user_f["finding_id"], 1)}
    listed = c.get(f"/artifacts?task_id={task_id}&kind=synthesis").json()["artifacts"]
    assert [a["artifact_id"] for a in listed] == [s["artifact_id"]]


def test_evidence_reads_stay_inside_the_project(api, tmp_path):
    store, c, task_id, project = api
    (project / "src").mkdir()
    (project / "src" / "auth.py").write_text("".join(f"line {n}\n" for n in range(1, 101)))
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    os.symlink(outside, project / "escape.txt")
    pid = c.get("/projects").json()["projects"][0]["project_id"]
    r = c.get(f"/projects/{pid}/file", params={"path": "src/auth.py", "range": "40-42"}).json()
    assert r == {
        "path": "src/auth.py",
        "first_line": 40,
        "lines": ["line 40", "line 41", "line 42"],
        "truncated": False,
    }
    for path in ("../outside.txt", "escape.txt", "/etc/hosts", "src/../../outside.txt"):
        assert c.get(f"/projects/{pid}/file", params={"path": path}).status_code == 403, path
    assert c.get(f"/projects/{pid}/file", params={"path": "nope.py"}).status_code == 404
    assert c.get(f"/projects/{pid}/file", params={"path": "src/auth.py", "range": "9-3"}).status_code == 422


def test_cli_compare_and_synthesize(api, capsys):
    from robin.cli import main
    from robin.service.main import write_endpoint

    store, c, task_id, _ = api
    write_endpoint(store, int(c.base_url.port))
    target = launch(c, task_id)
    agent_f, user_f = two_findings(c, task_id)
    d = ["--data", str(store.data_dir)]
    main(["compare", "--task", task_id, *d])
    out = capsys.readouterr().out
    assert out.count("== ") == 2 and "[src/auth.py:40-52]" in out and "[unsupported]" in out
    main(
        [
            "synthesize",
            "--task",
            task_id,
            "--to",
            target,
            "--findings",
            f"{agent_f['finding_id']},{user_f['finding_id']}",
            *d,
        ]
    )
    out = capsys.readouterr().out
    assert (
        "robin context deliver" in out
        and "Synthesize these findings" in c.get("/context").json()["packets"][0]["question"]
    )
