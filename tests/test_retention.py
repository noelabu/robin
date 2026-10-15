"""The storage cap on copied activity (TR-081)."""

import json

from sqlalchemy import select

from robin import retention, search
from robin.events.ingest import read_events, read_payload_json
from robin.store.models import Event, PrunedEvent
from test_context import observed_session_with, publish_user

BIG = "x" * (80 * 1024)  # over the inline limit: stored as a blob


def setup(store):
    old = observed_session_with(
        store,
        [("message.user", {"text": "ancient alpha"}), ("tool.result", {"tool": "shell", "output": BIG})],
    )
    search.catch_up(store)
    return old


def cap(store, gb):
    (store.data_dir / "settings.toml").write_text(f"storage_cap_gb = {gb}\n")


def test_usage_counts_copied_activity_only(api):
    store, c, task_id, _ = api
    setup(store)
    publish_user(c, task_id)
    search.catch_up(store)
    u = retention.usage(store)
    assert u.events_blobs >= len(BIG) and u.events_inline > 0 and u.search_text > 0
    assert u.protected > 0 and u.counted == u.events_inline + u.events_blobs + u.search_text
    assert u.cap == 20 * 1024**3 and u.state == "ok"
    cap(store, u.counted / 0.85 / 1024**3)
    assert retention.usage(store).state == "warning"  # 80% and above
    cap(store, u.counted / 2 / 1024**3)
    assert retention.usage(store).state == "over"


def test_pruning_is_oldest_first_and_keeps_identity(api):
    store, c, task_id, _ = api
    old = setup(store)
    f = publish_user(c, task_id)
    events = read_events(store, session_id=old)
    big = next(e for e in events if e.payload_ref)
    blob = store.blobs.path_for(big.payload_ref.split(":")[-1])
    assert blob.exists()
    before = {e.global_cursor: (e.content_hash, e.source_id, e.source_seq) for e in events}

    preview = retention.plan(store, target=0)
    assert preview["events"] == len(events) and preview["sessions"] == 1
    assert read_payload_json(store, big.global_cursor) != {"_robin_pruned": True}  # a plan changes nothing

    done = retention.prune(store, target=0)
    assert done["pruned_events"] == len(events) and done["removed_blobs"] == 1 and not blob.exists()
    after = read_events(store, session_id=old)
    assert {e.global_cursor: (e.content_hash, e.source_id, e.source_seq) for e in after} == before  # identity
    assert all(read_payload_json(store, e.global_cursor) == {"_robin_pruned": True} for e in after)
    with store.reader.connect() as conn:
        assert (
            conn.execute(
                select(PrunedEvent.original_size).where(PrunedEvent.global_cursor == big.global_cursor)
            ).scalar()
            == big.payload_size
        )
    assert search.search(store, "ancient")["results"] == []  # no longer searchable
    assert c.get(f"/findings/{f['finding_id']}").json()["summary"]  # findings are never pruned
    assert retention.plan(store, target=0)["events"] == 0  # nothing left to prune


def test_pruned_events_read_as_unavailable(api):
    store, c, task_id, _ = api
    from test_findings import launch

    old = setup(store)
    first = read_events(store, session_id=old)[0].global_cursor
    retention.prune(store, target=0)
    target = launch(c, task_id)
    p = c.post(
        "/context/preview",
        json={
            "task_id": task_id,
            "target_session_id": target,
            "question": "q",
            "excerpts": [{"session_id": old, "after": first - 1}],
            "include_objective": False,
        },
    ).json()
    assert "pruned to stay under Robin's storage cap" in p["text"] and "ancient" not in p["text"]


def test_enforce_prunes_only_when_over_the_cap(api):
    store, c, _, _ = api
    setup(store)
    assert retention.enforce(store) is None
    u = retention.usage(store)
    cap(store, u.counted / 2 / 1024**3)
    done = retention.enforce(store)
    assert done is not None and done["after"]["counted"] <= retention.PRUNE_TO * done["after"]["cap"]


def test_cli_previews_before_pruning(api, capsys, monkeypatch):
    import io

    from robin.cli import main
    from robin.service.main import write_endpoint

    store, c, _, _ = api
    write_endpoint(store, int(c.base_url.port))
    setup(store)
    u = retention.usage(store)
    cap(store, u.counted / 2 / 1024**3)
    d = ["--data", str(store.data_dir)]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["storage", "prune", *d])
    out = capsys.readouterr().out
    assert "would prune" in out and "nothing pruned" in out
    with store.reader.connect() as conn:
        assert conn.execute(select(PrunedEvent)).first() is None
    main(["storage", "prune", "--yes", *d])
    assert "pruned" in capsys.readouterr().out
    assert json.loads(c.get("/storage").content)["state"] in ("ok", "warning")
    with store.reader.connect() as conn:
        assert conn.execute(select(Event.global_cursor)).first() is not None  # rows stay
