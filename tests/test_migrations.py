import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from robin.store import StoreLocked, open_store
from robin.store.migrate import MIGRATIONS, MigrationFailed, SchemaTooNew, alembic_config, current_revision
from robin.store.models import Base

HEAD = ScriptDirectory.from_config(alembic_config()).get_current_head()

FAILING_REVISION = """
from alembic import op
revision = "9000_fails"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE half_done (x INTEGER)")
    raise RuntimeError("simulated crash mid-migration")


def downgrade():
    pass
"""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}


def test_fresh_db_at_head_without_backup(data_dir):
    s = open_store(data_dir)
    try:
        assert current_revision(s.db_path) == HEAD
        assert s.backup_path is None
    finally:
        s.close()


def test_models_match_migrations(store):
    """The frozen baseline DDL and the SQLAlchemy models describe the same schema."""
    engine = create_engine(f"sqlite:///{store.db_path}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_pragmas_wal_and_foreign_keys(store):
    with store.reader.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_refuses_newer_schema_without_writes(data_dir):
    open_store(data_dir).close()
    db = data_dir / "robin.db"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE alembic_version SET version_num='9999_future'")
    before = _digest(db)
    with pytest.raises(SchemaTooNew):
        open_store(data_dir)
    assert _digest(db) == before
    assert not (data_dir / "backups").exists()


def test_interrupted_migration_rolls_back_after_backup(data_dir, tmp_path):
    open_store(data_dir).close()
    scripts = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "9000_fails.py").write_text(FAILING_REVISION.replace("{head}", HEAD))
    with pytest.raises(MigrationFailed, match="backup"):
        open_store(data_dir, migrations=scripts)
    db = data_dir / "robin.db"
    assert current_revision(db) == HEAD
    assert "half_done" not in _tables(db)
    backups = list((data_dir / "backups").glob(f"robin-{HEAD}-*.db"))
    assert len(backups) == 1 and "event" in _tables(backups[0])
    assert not list((data_dir / "backups").glob("*.partial"))
    open_store(data_dir).close()  # the real head still opens


def test_second_open_blocked_by_lock(store):
    with pytest.raises(StoreLocked):
        open_store(store.data_dir)


def test_upgrade_from_baseline_keeps_rows_and_backs_up(data_dir, tmp_path):
    """A database created at 0001 upgrades to head with its rows intact and a backup taken first."""
    import shutil

    scripts = tmp_path / "only-baseline"
    shutil.copytree(MIGRATIONS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    for p in (scripts / "versions").glob("*.py"):
        if not p.name.startswith("0001_"):
            p.unlink()
    open_store(data_dir, migrations=scripts).close()
    with sqlite3.connect(data_dir / "robin.db") as c:
        c.execute("INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
    s = open_store(data_dir)
    try:
        assert current_revision(s.db_path) == HEAD and s.backup_path is not None
        with sqlite3.connect(s.db_path) as c:
            assert c.execute("SELECT value FROM meta WHERE key='probe'").fetchone() == ("kept",)
    finally:
        s.close()
