import threading

import pytest
from sqlalchemy import insert, select, text

from robin.store.models import Meta
from robin.store.writer import WriterBusy, WriterClosed


def test_result_after_commit(store):
    seen_by_reader = []

    def work(conn):
        conn.execute(insert(Meta).values(key="k1", value="v1"))
        return "done"

    assert store.writer.call(work) == "done"
    with store.reader.connect() as c:
        seen_by_reader.append(c.execute(select(Meta.value).where(Meta.key == "k1")).scalar_one())
    assert seen_by_reader == ["v1"]


def test_exception_rolls_back_and_writer_continues(store):
    def fails(conn):
        conn.execute(insert(Meta).values(key="k2", value="v"))
        raise ValueError("boom")

    with pytest.raises(ValueError):
        store.writer.call(fails)
    assert store.writer.call(lambda c: c.execute(select(Meta.key).where(Meta.key == "k2")).first()) is None
    assert store.writer.call(lambda c: 1) == 1


def test_backpressure_raises_writer_busy(store):
    gate = threading.Event()
    blocker = store.writer.submit(lambda c: gate.wait(5))
    # fill the queue (maxsize 256) without letting the writer drain
    with pytest.raises(WriterBusy):
        for _ in range(300):
            store.writer.submit(lambda c: None, block=False)
    gate.set()
    blocker.result(5)


async def test_async_acall(store):
    assert await store.writer.acall(lambda c: c.execute(text("select 41 + 1")).scalar()) == 42


def test_close_drains_then_rejects(data_dir):
    from robin.store import open_store

    s = open_store(data_dir)
    futures = [s.writer.submit(lambda c, i=i: i) for i in range(20)]
    s.close()
    assert [f.result(1) for f in futures] == list(range(20))
    with pytest.raises(WriterClosed):
        s.writer.submit(lambda c: None)


def test_failed_open_stops_writer_and_releases_lock(data_dir, monkeypatch):
    import robin.store.lifecycle as lifecycle
    from robin.store import open_store

    def broken_reconcile(writer, blobs):
        raise OSError("disk went away")

    monkeypatch.setattr(lifecycle, "_reconcile_blobs", broken_reconcile)
    with pytest.raises(OSError):
        open_store(data_dir)
    assert not [t for t in threading.enumerate() if t.name == "robin-writer"]
    monkeypatch.undo()
    open_store(data_dir).close()  # the lock was released
