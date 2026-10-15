"""Full-text search: masked indexing, incremental watermark, exclusions, documents, safe queries (FR-040)."""

import time

import pytest

from robin import search
from robin.events.ingest import read_events
from robin.workspace import tasks as ws
from test_context import FAKE_TOKEN, observed_session_with, publish_user


def found(store, q, **kw):
    return search.search(store, q, **kw)["results"]


def test_events_are_indexed_masked_and_incrementally(store):
    sid = observed_session_with(
        store,
        [
            ("message.user", {"text": "please rotate the deploy credentials"}),
            ("tool.result", {"tool": "shell", "output": f"GITHUB_TOKEN={FAKE_TOKEN}"}),
            ("unknown.raw", {"text": "not indexed: unknown records"}),
        ],
    )
    assert search.lag(store) > 0
    search.catch_up(store)
    assert search.lag(store) == 0
    (hit,) = found(store, "rotate deploy")
    assert hit["kind"] == "event" and hit["session_id"] == sid and "[rotate]" in hit["snippet"]
    (tool,) = found(store, "GITHUB_TOKEN")
    assert "[REDACTED:github-pat]" in tool["snippet"] and FAKE_TOKEN not in tool["snippet"]
    assert found(store, FAKE_TOKEN[:12]) == []  # the secret itself was never indexed
    assert found(store, "unknown records") == []

    search.catch_up(store)  # nothing new: no duplicates
    assert len(found(store, "rotate")) == 1


def test_exclusions_from_the_config_file(store, tmp_path):
    (store.data_dir / "index.toml").write_text(
        f'exclude_paths = ["{tmp_path}/private*"]\nindex_tool_output = false\n'
    )
    from robin.events.ingest import ensure_session

    hidden = ensure_session(
        store,
        runtime="claude",
        mode="observed",
        native_session_id="priv",
        execution_cwd=str(tmp_path / "private-repo"),
    )
    observed_session_with(
        store,
        [
            ("tool.result", {"tool": "shell", "output": "compiled okay banana"}),
            ("message.user", {"text": "visible banana"}),
        ],
    )
    from robin.events.envelope import EvidenceKind, NewEvent
    from robin.events.ingest import CheckpointAdvance, ingest_batch, open_generation

    store.writer.call(
        lambda c: open_generation(
            c, source_id="t:priv", kind="jsonl_file", runtime="claude", reason="initial", session_id=hidden
        )
    )
    ingest_batch(
        store,
        [
            NewEvent(
                runtime="claude",
                session_id=hidden,
                source_id="t:priv",
                source_generation=0,
                source_event_id="p1",
                source_seq=1,
                event_type="message.user",
                evidence_kind=EvidenceKind.REPORTED,
                payload=b'{"text": "secret banana"}',
            )
        ],
        [],
        CheckpointAdvance("t:priv", 0, 1, None),
    )
    search.catch_up(store)
    assert [h["snippet"] for h in found(store, "banana")] == ["visible [banana]"]


def test_documents_are_indexed_and_reindexed_on_change(api):
    store, c, task_id, _ = api
    publish_user(c, task_id)
    search.catch_up(store)
    assert {h["kind"] for h in found(store, "bugs")} >= {"task"}
    assert found(store, "revoked token", kind="finding")[0]["task_id"] == task_id
    task = ws.get_task(store, task_id)
    time.sleep(0.01)
    ws.update_task(store, task_id, expected_revision=task["revision"], objective="Audit the payment flow")
    search.catch_up(store)
    assert found(store, "payment", kind="task") and not found(store, "bugs", kind="task")
    from test_findings import launch

    sid = launch(c, task_id)
    time.sleep(0.01)
    search.catch_up(store)
    assert sid in [h["session_id"] for h in found(store, "review project", kind="session")]  # by folder


@pytest.mark.parametrize("q", ["NEAR(a b)", '"unterminated', "a OR b", "col:value", "*", "-x", "a AND NOT b"])
def test_user_queries_never_reach_fts_syntax(store, q):
    observed_session_with(store, [("message.user", {"text": "a b x value"})])
    search.catch_up(store)
    try:
        search.search(store, q)
    except ValueError:
        assert q == "*"  # no words at all


def test_paging_is_bounded(store):
    observed_session_with(store, [("message.user", {"text": f"needle {i}"}) for i in range(130)])
    search.catch_up(store)
    first = search.search(store, "needle", limit=500)
    assert len(first["results"]) == 100 and first["more"] and first["next_offset"] == 100
    rest = search.search(store, "needle", limit=100, offset=100)
    assert len(rest["results"]) == 30 and not rest["more"]


def test_the_service_indexes_in_the_background(api, capsys):
    from robin.cli import main
    from robin.service.main import write_endpoint
    from test_findings import wait

    store, c, task_id, _ = api
    observed_session_with(store, [("message.user", {"text": "zebra migration plan"})])
    wait(lambda: c.get("/search", params={"q": "zebra"}).json()["results"])
    assert c.get("/search", params={"q": "!!!"}).status_code == 422
    write_endpoint(store, int(c.base_url.port))
    main(["search", "zebra", "plan", "--data", str(store.data_dir)])
    assert "[zebra]" in capsys.readouterr().out
    assert read_events(store)  # sanity


def test_cli_pages_with_offset(api, capsys):
    from robin.cli import main
    from robin.service.main import write_endpoint
    from test_findings import wait

    store, c, task_id, _ = api
    write_endpoint(store, int(c.base_url.port))
    observed_session_with(store, [("message.user", {"text": f"quokka {i}"}) for i in range(3)])
    wait(lambda: len(c.get("/search", params={"q": "quokka"}).json()["results"]) == 3)
    d = ["--data", str(store.data_dir)]
    main(["search", "quokka", "--limit", "2", *d])
    out = capsys.readouterr().out
    assert out.count("[quokka]") == 2 and "--offset 2" in out
    main(["search", "quokka", "--limit", "2", "--offset", "2", *d])
    assert capsys.readouterr().out.count("[quokka]") == 1
