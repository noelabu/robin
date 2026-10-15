from sqlalchemy import insert, select

from robin.ids import utc_now
from robin.store import open_store
from robin.store.blobs import BlobStore, sha256_hex
from robin.store.models import AuditEntry, Blob


def test_put_atomic_and_idempotent(tmp_path):
    blobs = BlobStore(tmp_path / "artifacts")
    sha = blobs.put(b"hello")
    assert sha == sha256_hex(b"hello")
    assert blobs.read(sha) == b"hello"
    assert blobs.put(b"hello") == sha
    assert [p.name for p in blobs.files()] == [sha]


def test_put_large_payload(tmp_path):
    blobs = BlobStore(tmp_path / "artifacts")
    data = bytes(range(256)) * (4 * 4096)
    assert blobs.read(blobs.put(data)) == data


def test_reconcile_removes_tmp_and_unreferenced(tmp_path):
    blobs = BlobStore(tmp_path / "artifacts")
    keep, orphan = blobs.put(b"keep"), blobs.put(b"orphan")
    tmp = blobs.path_for(keep).parent / f".{keep}.tmp-dead"
    tmp.write_bytes(b"partial")
    removed, missing = blobs.reconcile({keep})
    assert removed == 2 and missing == []
    assert blobs.exists(keep) and not blobs.exists(orphan) and not tmp.exists()


def test_reconcile_on_open_marks_missing_ref(data_dir):
    s = open_store(data_dir)
    sha = s.blobs.put(b"will vanish")
    s.writer.call(
        lambda c: c.execute(
            insert(Blob).values(sha256=sha, size=11, media_type="application/json", created_at=utc_now())
        )
    )
    s.close()
    BlobStore(data_dir / "artifacts").path_for(sha).unlink()
    s = open_store(data_dir)
    try:
        with s.reader.connect() as c:
            assert c.execute(select(Blob.state).where(Blob.sha256 == sha)).scalar_one() == "missing"
            assert c.execute(select(AuditEntry.outcome)).scalar_one() == "missing_on_disk"
    finally:
        s.close()
