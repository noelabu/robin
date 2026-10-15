"""Hook relays as an ingestion source (FR-041, TR-076): allow source kind 'hook'.

SQLite can't change a CHECK constraint in place, so source_checkpoint is rebuilt: create, copy, drop, rename.
The runner turns foreign keys off for the upgrade and checks them before commit, so events keep their
references to the renamed table.

Revision ID: 0005_hook_source
Revises: 0004_attention
Create Date: 2026-10-15
"""

from alembic import op

revision = "0005_hook_source"
down_revision = "0004_attention"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE source_checkpoint__new (
        source_id VARCHAR NOT NULL,
        generation INTEGER NOT NULL,
        kind VARCHAR NOT NULL,
        generation_label VARCHAR,
        session_id VARCHAR,
        runtime VARCHAR NOT NULL,
        path TEXT,
        file_dev INTEGER,
        file_ino INTEGER,
        "offset" INTEGER DEFAULT 0 NOT NULL,
        anchor_hash VARCHAR,
        state VARCHAR NOT NULL,
        reason VARCHAR NOT NULL,
        created_at VARCHAR(27) NOT NULL,
        updated_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_source_checkpoint PRIMARY KEY (source_id, generation),
        CONSTRAINT ck_source_checkpoint_kind CHECK (kind IN ('supervisor', 'jsonl_file', 'hook')),
        CONSTRAINT ck_source_checkpoint_state CHECK (state IN ('active', 'superseded')),
        CONSTRAINT ck_source_checkpoint_reason CHECK (reason IN ('initial', 'rotated', 'truncated', 'rewritten')),
        CONSTRAINT fk_source_checkpoint_session_id_session FOREIGN KEY(session_id) REFERENCES session (session_id)
    )
    """,
    """
    INSERT INTO source_checkpoint__new (
        "source_id",
        "generation",
        "kind",
        "generation_label",
        "session_id",
        "runtime",
        "path",
        "file_dev",
        "file_ino",
        "offset",
        "anchor_hash",
        "state",
        "reason",
        "created_at",
        "updated_at"
    )
    SELECT
        "source_id",
        "generation",
        "kind",
        "generation_label",
        "session_id",
        "runtime",
        "path",
        "file_dev",
        "file_ino",
        "offset",
        "anchor_hash",
        "state",
        "reason",
        "created_at",
        "updated_at"
    FROM source_checkpoint
    """,
    """
    DROP TABLE source_checkpoint
    """,
    """
    ALTER TABLE source_checkpoint__new RENAME TO source_checkpoint
    """,
    """
    CREATE UNIQUE INDEX ix_source_checkpoint_active ON source_checkpoint (source_id) WHERE state = 'active'
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
